import re
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from typing import Any

from pydantic import BaseModel


SENSITIVE_TOKEN_SEQUENCES = {
    ("access", "token"),
    ("api", "key"),
    ("bearer", "token"),
    ("client", "secret"),
    ("private", "key"),
    ("refresh", "token"),
}
SENSITIVE_SINGLE_TOKENS = {"authorization", "password", "secret", "token"}


class InlineCredentialsForbiddenError(ValueError):
    code = "inline_credentials_forbidden"
    http_status = 400


def redact_sensitive_data(
    value: Any,
    *,
    schema: dict[str, Any] | None = None,
    replacement: str = "***",
) -> Any:
    root_schema = schema or {}
    return _redact(
        value,
        schemas=[root_schema],
        root_schema=root_schema,
        replacement=replacement,
    )


def contains_inline_credentials(value: Any) -> bool:
    value = _to_plain_value(value)
    if isinstance(value, Mapping):
        return any(
            _is_sensitive_key(str(key)) or contains_inline_credentials(nested)
            for key, nested in value.items()
        )
    if _is_sequence(value):
        return any(contains_inline_credentials(item) for item in value)
    return False


def reject_inline_credentials(value: Any) -> None:
    if contains_inline_credentials(value):
        raise InlineCredentialsForbiddenError(
            "工具参数不得包含内联连接凭证，请由 Gateway 或 Adapter 注入。"
        )


def _redact(
    value: Any,
    *,
    schemas: list[dict[str, Any]],
    root_schema: dict[str, Any],
    replacement: str,
) -> Any:
    value = _to_plain_value(value)
    expanded = _expand_schemas(schemas, root_schema=root_schema)
    if any(_schema_marks_sensitive(candidate) for candidate in expanded):
        return replacement
    if isinstance(value, Mapping):
        return {
            key: replacement
            if _is_sensitive_key(str(key))
            else _redact(
                nested,
                schemas=_child_schemas(expanded, str(key)),
                root_schema=root_schema,
                replacement=replacement,
            )
            for key, nested in value.items()
        }
    if _is_sequence(value):
        item_schemas = [
            item_schema
            for candidate in expanded
            for item_schema in [candidate.get("items")]
            if isinstance(item_schema, dict)
        ]
        items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
        return [
            _redact(
                item,
                schemas=item_schemas,
                root_schema=root_schema,
                replacement=replacement,
            )
            for item in items
        ]
    return value


def _expand_schemas(
    schemas: list[dict[str, Any]],
    *,
    root_schema: dict[str, Any],
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    pending = [(schema, frozenset()) for schema in schemas if isinstance(schema, dict)]
    while pending:
        schema, seen_refs = pending.pop()
        expanded.append(schema)
        reference = schema.get("$ref")
        if isinstance(reference, str) and reference not in seen_refs:
            resolved = _resolve_local_ref(reference, root_schema)
            if resolved is not None:
                pending.append((resolved, seen_refs | {reference}))
        for keyword in ("allOf", "anyOf", "oneOf"):
            branches = schema.get(keyword, [])
            if isinstance(branches, list):
                pending.extend(
                    (branch, seen_refs)
                    for branch in branches
                    if isinstance(branch, dict)
                )
    return expanded


def _child_schemas(
    schemas: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for schema in schemas:
        matched = False
        properties = schema.get("properties")
        if isinstance(properties, dict) and isinstance(properties.get(key), dict):
            children.append(properties[key])
            matched = True
        pattern_properties = schema.get("patternProperties")
        if isinstance(pattern_properties, dict):
            for pattern, child_schema in pattern_properties.items():
                if isinstance(child_schema, dict) and re.search(pattern, key):
                    children.append(child_schema)
                    matched = True
        additional = schema.get("additionalProperties")
        if not matched and isinstance(additional, dict):
            children.append(additional)
    return children


def _resolve_local_ref(
    reference: str,
    root_schema: dict[str, Any],
) -> dict[str, Any] | None:
    if not reference.startswith("#/"):
        return None
    resolved: Any = root_schema
    for segment in reference[2:].split("/"):
        if not isinstance(resolved, dict):
            return None
        key = segment.replace("~1", "/").replace("~0", "~")
        resolved = resolved.get(key)
    return resolved if isinstance(resolved, dict) else None


def _schema_marks_sensitive(schema: dict[str, Any]) -> bool:
    return schema.get("writeOnly") is True or schema.get("x-sensitive") is True


def _is_sensitive_key(key: str) -> bool:
    tokens = _key_tokens(key)
    if len(tokens) == 1:
        return tokens[0] in SENSITIVE_SINGLE_TOKENS
    if tokens and tokens[-1] in {"password", "secret"}:
        return True
    return any(_contains_sequence(tokens, phrase) for phrase in SENSITIVE_TOKEN_SEQUENCES)


def _key_tokens(key: str) -> tuple[str, ...]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return tuple(part for part in re.split(r"[^a-zA-Z0-9]+", separated.lower()) if part)


def _contains_sequence(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    return any(tokens[index : index + width] == phrase for index in range(len(tokens)))


def _to_plain_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    return value


def _is_sequence(value: Any) -> bool:
    return (
        isinstance(value, (Sequence, set, frozenset))
        and not isinstance(value, (str, bytes, bytearray))
    )
