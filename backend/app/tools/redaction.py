import re
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel


SENSITIVE_TOKEN_SEQUENCES = {
    ("access", "token"),
    ("api", "key"),
    ("bearer", "token"),
    ("client", "secret"),
    ("connection", "string"),
    ("database", "url"),
    ("private", "key"),
    ("refresh", "token"),
}
SENSITIVE_SINGLE_TOKENS = {
    "authorization",
    "credential",
    "credentials",
    "dsn",
    "password",
    "secret",
    "token",
}
URI_USERINFO_PATTERN = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@",
    re.IGNORECASE,
)
BEARER_PATTERN = re.compile(r"\bbearer\s+[a-z0-9._~+/=-]{8,}", re.IGNORECASE)
API_KEY_ASSIGNMENT_PATTERN = re.compile(
    r"\bapi[\s_-]*key\s*[:=]\s*[a-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)
JWT_PATTERN = re.compile(
    r"\b[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}\b",
    re.IGNORECASE,
)
URL_CANDIDATE_PATTERN = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+",
    re.IGNORECASE,
)
AUTHORIZATION_HEADER_PATTERN = re.compile(
    r"\b(?:proxy[-_\s]*)?authorization\s*:\s*(?:basic|bearer)\s+[^\s,;]+",
    re.IGNORECASE,
)
COLON_CREDENTIAL_PATTERN = re.compile(
    r"\b(?:client[-_\s]*secret|database[-_\s]*password)\s*:\s*[^\s,;]+",
    re.IGNORECASE,
)
TRACE_URI_USERINFO_PATTERN = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@",
    re.IGNORECASE,
)
TRACE_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?P<key>[a-z][a-z0-9_.-]{1,64})\s*=\s*[^\s,;]+",
    re.IGNORECASE,
)


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
        active_object_ids=frozenset(),
    )


def contains_inline_credentials(value: Any) -> bool:
    return _contains_inline_credentials(value, active_object_ids=frozenset())


def reject_inline_credentials(value: Any) -> None:
    if contains_inline_credentials(value):
        raise InlineCredentialsForbiddenError(
            "工具参数不得包含内联连接凭证，请由 Gateway 或 Adapter 注入。"
        )


def reject_server_injected_arguments(
    value: Any,
    *,
    schema: dict[str, Any],
    server_injected_fields: Sequence[str] = (),
) -> None:
    normalized_injected = {_normalized_key(name) for name in server_injected_fields}
    if _contains_server_injected_value(
        value,
        schemas=[schema],
        root_schema=schema,
        server_injected_fields=normalized_injected,
        active_object_ids=frozenset(),
    ):
        raise InlineCredentialsForbiddenError(
            "工具参数包含只能由 Gateway 或 Adapter 注入的字段。"
        )


def sanitize_trace_error(value: str | None, *, max_length: int = 512) -> str | None:
    if value is None:
        return None
    sanitized = TRACE_URI_USERINFO_PATTERN.sub(r"\g<scheme>***@", value)
    sanitized = AUTHORIZATION_HEADER_PATTERN.sub("Authorization: ***", sanitized)
    sanitized = COLON_CREDENTIAL_PATTERN.sub("credential: ***", sanitized)
    sanitized = BEARER_PATTERN.sub("Bearer ***", sanitized)
    sanitized = JWT_PATTERN.sub("***", sanitized)
    sanitized = TRACE_ASSIGNMENT_PATTERN.sub(r"\g<key>=***", sanitized)
    return sanitized[:max_length]


def _contains_inline_credentials(
    value: Any,
    *,
    active_object_ids: frozenset[int],
) -> bool:
    value = _to_plain_value(value)
    if isinstance(value, str):
        return _contains_credential_text(value)
    if not _is_container(value):
        return False
    object_id = id(value)
    if object_id in active_object_ids:
        return True
    nested_ids = active_object_ids | {object_id}
    if isinstance(value, Mapping):
        return any(
            _is_sensitive_key(str(key))
            or _contains_inline_credentials(
                nested,
                active_object_ids=nested_ids,
            )
            for key, nested in value.items()
        )
    return any(
        _contains_inline_credentials(item, active_object_ids=nested_ids)
        for item in value
    )


def _redact(
    value: Any,
    *,
    schemas: list[dict[str, Any]],
    root_schema: dict[str, Any],
    replacement: str,
    active_object_ids: frozenset[int],
) -> Any:
    value = _to_plain_value(value)
    expanded, schema_failed_closed = _expand_schemas(
        schemas,
        root_schema=root_schema,
    )
    if schema_failed_closed or any(
        _schema_marks_sensitive(candidate) for candidate in expanded
    ):
        return replacement
    if not _is_container(value):
        return value
    object_id = id(value)
    if object_id in active_object_ids:
        return replacement
    nested_ids = active_object_ids | {object_id}
    if isinstance(value, Mapping):
        return {
            key: replacement
            if _is_sensitive_key(str(key))
            else _redact(
                nested,
                schemas=_child_schemas(expanded, str(key)),
                root_schema=root_schema,
                replacement=replacement,
                active_object_ids=nested_ids,
            )
            for key, nested in value.items()
        }
    items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
    return [
        _redact(
            item,
            schemas=_item_schemas(expanded, index),
            root_schema=root_schema,
            replacement=replacement,
            active_object_ids=nested_ids,
        )
        for index, item in enumerate(items)
    ]


def _contains_server_injected_value(
    value: Any,
    *,
    schemas: list[dict[str, Any]],
    root_schema: dict[str, Any],
    server_injected_fields: set[str],
    active_object_ids: frozenset[int],
) -> bool:
    value = _to_plain_value(value)
    expanded, schema_failed_closed = _expand_schemas(
        schemas,
        root_schema=root_schema,
    )
    if schema_failed_closed or any(
        _schema_marks_sensitive(candidate) for candidate in expanded
    ):
        return True
    if not _is_container(value):
        return False
    object_id = id(value)
    if object_id in active_object_ids:
        return True
    nested_ids = active_object_ids | {object_id}
    if isinstance(value, Mapping):
        return any(
            _normalized_key(str(key)) in server_injected_fields
            or _contains_server_injected_value(
                nested,
                schemas=_child_schemas(expanded, str(key)),
                root_schema=root_schema,
                server_injected_fields=server_injected_fields,
                active_object_ids=nested_ids,
            )
            for key, nested in value.items()
        )
    return any(
        _contains_server_injected_value(
            item,
            schemas=_item_schemas(expanded, index),
            root_schema=root_schema,
            server_injected_fields=server_injected_fields,
            active_object_ids=nested_ids,
        )
        for index, item in enumerate(value)
    )


def _expand_schemas(
    schemas: list[dict[str, Any]],
    *,
    root_schema: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    expanded: list[dict[str, Any]] = []
    failed_closed = False
    pending = [(schema, frozenset()) for schema in schemas if isinstance(schema, dict)]
    while pending:
        schema, seen_refs = pending.pop()
        expanded.append(schema)
        if _has_unknown_security_shape(schema):
            failed_closed = True
        reference = schema.get("$ref")
        if isinstance(reference, str) and reference not in seen_refs:
            resolved = _resolve_local_ref(reference, root_schema)
            if reference.startswith("#/") and resolved is None:
                failed_closed = True
            elif resolved is not None:
                pending.append((resolved, seen_refs | {reference}))
        for keyword in ("allOf", "anyOf", "oneOf"):
            branches = schema.get(keyword, [])
            if isinstance(branches, list):
                pending.extend(
                    (branch, seen_refs)
                    for branch in branches
                    if isinstance(branch, dict)
                )
    return expanded, failed_closed


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


def _item_schemas(
    schemas: list[dict[str, Any]],
    index: int,
) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for schema in schemas:
        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list) and index < len(prefix_items):
            child = prefix_items[index]
            if isinstance(child, dict):
                children.append(child)
            continue
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            children.append(item_schema)
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


def _has_unknown_security_shape(schema: dict[str, Any]) -> bool:
    for key, value in schema.items():
        normalized = key.lower().replace("_", "-")
        if normalized in {"writeonly", "write-only", "x-sensitive"}:
            if value is not True and value is not False:
                return True
            continue
        if any(term in normalized for term in ("credential", "secret", "sensitive")):
            return True
    return False


def _contains_credential_text(value: str) -> bool:
    if _contains_url_credentials(value):
        return True
    return any(
        pattern.search(value)
        for pattern in (
            URI_USERINFO_PATTERN,
            BEARER_PATTERN,
            API_KEY_ASSIGNMENT_PATTERN,
            JWT_PATTERN,
            AUTHORIZATION_HEADER_PATTERN,
            COLON_CREDENTIAL_PATTERN,
        )
    )


def _contains_url_credentials(value: str) -> bool:
    for match in URL_CANDIDATE_PATTERN.finditer(value):
        candidate = match.group(0).rstrip(".,);]")
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.username is not None or parsed.password is not None:
            return True
        try:
            query_items = parse_qsl(parsed.query, keep_blank_values=True)
        except ValueError:
            continue
        if any(_is_sensitive_key(key) for key, _ in query_items):
            return True
    return False


def _is_sensitive_key(key: str) -> bool:
    tokens = _key_tokens(key)
    if len(tokens) == 1:
        return tokens[0] in SENSITIVE_SINGLE_TOKENS
    if any(token in {"authorization", "credential", "credentials", "dsn"} for token in tokens):
        return True
    if tokens and tokens[-1] in {"password", "secret"}:
        return True
    if tokens and tokens[-1] == "token":
        return True
    return any(_contains_sequence(tokens, phrase) for phrase in SENSITIVE_TOKEN_SEQUENCES)


def _key_tokens(key: str) -> tuple[str, ...]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return tuple(part for part in re.split(r"[^a-zA-Z0-9]+", separated.lower()) if part)


def _normalized_key(key: str) -> str:
    return "".join(_key_tokens(key))


def _contains_sequence(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    return any(tokens[index : index + width] == phrase for index in range(len(tokens)))


def _to_plain_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    return value


def _is_container(value: Any) -> bool:
    return isinstance(value, Mapping) or _is_sequence(value)


def _is_sequence(value: Any) -> bool:
    return (
        isinstance(value, (Sequence, set, frozenset))
        and not isinstance(value, (str, bytes, bytearray))
    )
