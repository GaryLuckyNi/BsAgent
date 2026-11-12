import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from math import isfinite
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlsplit

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
# 这些名字只在 URL 参数中视为凭证，普通业务 JSON 的 key/auth 不因此变敏感。
URL_CREDENTIAL_KEYS = frozenset({"auth", "key", "signature", "cookie", "passwd"})
URL_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?:^|[?&#\s\"'\\{,])"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|"
    r"password|passwd|secret|token|signature|authorization|auth|key|cookie)"
    r"[\\\"']*\s*[:=]",
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
DETAIL_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?P<key>"
    r"access[\s_.-]*token|api[\s_.-]*key|authorization|bearer[\s_.-]*token|"
    r"client[\s_.-]*secret|connection[\s_.-]*string|credential(?:s)?|"
    r"database[\s_.-]*(?:password|url)|dsn|password|private[\s_.-]*key|"
    r"refresh[\s_.-]*token|secret|token"
    r")(?P<separator>\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)

DETAIL_DEPTH_LIMIT = "[depth_limit]"
DETAIL_NODE_LIMIT = "[node_limit]"
DETAIL_COLLECTION_LIMIT = "[collection_limit]"
DETAIL_STRING_LIMIT = "[string_limit]"
DETAIL_TRUNCATED_KEY = "__truncated__"


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
    sanitized = _sanitize_trace_text(value)
    return sanitized[:max_length]


def sanitize_untrusted_details(
    details: Mapping[str, Any] | None,
    *,
    replacement: str = "***",
    max_depth: int = 8,
    max_nodes: int = 256,
    max_collection_items: int = 100,
    max_string_length: int = 2048,
) -> dict[str, Any]:
    """将外部错误详情收敛为递归脱敏的 JSON-safe dict。"""

    budget = _DetailSanitizationBudget(remaining_nodes=max(0, max_nodes))
    sanitized = _sanitize_detail_value(
        details or {},
        replacement=replacement,
        depth=0,
        max_depth=max(0, max_depth),
        max_collection_items=max(0, max_collection_items),
        max_string_length=max(0, max_string_length),
        budget=budget,
        active_object_ids=frozenset(),
    )
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_trace_text(value: str) -> str:
    sanitized = TRACE_URI_USERINFO_PATTERN.sub(r"\g<scheme>***@", value)
    sanitized = AUTHORIZATION_HEADER_PATTERN.sub("Authorization: ***", sanitized)
    sanitized = COLON_CREDENTIAL_PATTERN.sub("credential: ***", sanitized)
    sanitized = BEARER_PATTERN.sub("Bearer ***", sanitized)
    sanitized = JWT_PATTERN.sub("***", sanitized)
    sanitized = TRACE_ASSIGNMENT_PATTERN.sub(r"\g<key>=***", sanitized)
    return sanitized


def _sanitize_untrusted_detail_text(value: str, *, max_length: int) -> str:
    if len(value) > max_length:
        value = value[:max_length] + DETAIL_STRING_LIMIT
    sanitized = TRACE_URI_USERINFO_PATTERN.sub(r"\g<scheme>***@", value)
    sanitized = AUTHORIZATION_HEADER_PATTERN.sub("Authorization: ***", sanitized)
    sanitized = BEARER_PATTERN.sub("Bearer ***", sanitized)
    sanitized = JWT_PATTERN.sub("***", sanitized)
    return DETAIL_SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('key')}{match.group('separator')}***",
        sanitized,
    )


@dataclass
class _DetailSanitizationBudget:
    remaining_nodes: int

    def consume(self) -> bool:
        if self.remaining_nodes <= 0:
            return False
        self.remaining_nodes -= 1
        return True


def _sanitize_detail_value(
    value: Any,
    *,
    replacement: str,
    depth: int,
    max_depth: int,
    max_collection_items: int,
    max_string_length: int,
    budget: _DetailSanitizationBudget,
    active_object_ids: frozenset[int],
) -> Any:
    if not budget.consume():
        return DETAIL_NODE_LIMIT
    if depth > max_depth:
        return DETAIL_DEPTH_LIMIT
    value = _to_plain_value(value)
    if isinstance(value, str):
        return _sanitize_untrusted_detail_text(
            value,
            max_length=max_string_length,
        )
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else replacement
    if not _is_container(value):
        return replacement

    object_id = id(value)
    if object_id in active_object_ids:
        return replacement
    nested_ids = active_object_ids | {object_id}
    if isinstance(value, Mapping):
        sanitized_mapping: dict[str, Any] = {}
        for index, (key, nested) in enumerate(value.items()):
            if index >= max_collection_items:
                sanitized_mapping[DETAIL_TRUNCATED_KEY] = DETAIL_COLLECTION_LIMIT
                break
            safe_key = _sanitize_untrusted_detail_text(
                str(key),
                max_length=max_string_length,
            )
            sanitized_mapping[safe_key] = (
                replacement
                if _is_sensitive_key(str(key))
                else _sanitize_detail_value(
                    nested,
                    replacement=replacement,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_collection_items=max_collection_items,
                    max_string_length=max_string_length,
                    budget=budget,
                    active_object_ids=nested_ids,
                )
            )
        return sanitized_mapping

    sanitized_items: list[Any] = []
    for index, item in enumerate(value):
        if index >= max_collection_items:
            sanitized_items.append(DETAIL_COLLECTION_LIMIT)
            break
        sanitized_items.append(
            _sanitize_detail_value(
                item,
                replacement=replacement,
                depth=depth + 1,
                max_depth=max_depth,
                max_collection_items=max_collection_items,
                max_string_length=max_string_length,
                budget=budget,
                active_object_ids=nested_ids,
            )
        )
    return sanitized_items


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
        for _round in range(4):
            if URL_CREDENTIAL_ASSIGNMENT_PATTERN.search(candidate):
                return True
            try:
                parsed = urlsplit(candidate)
                if parsed.username is not None or parsed.password is not None:
                    return True
                for parameters in (
                    parsed.query,
                    parsed.fragment,
                    parsed.fragment.partition("?")[2],
                ):
                    if any(
                        _is_sensitive_key(key) or key.casefold() in URL_CREDENTIAL_KEYS
                        for key, _ in parse_qsl(parameters, keep_blank_values=True)
                    ):
                        return True
                decoded = unquote_plus(candidate, errors="strict")
            except (ValueError, UnicodeError):
                # 无法完成 URL 凭证检查时，不能声称原文不含凭证。
                return True
            if decoded == candidate:
                break
            candidate = decoded
        else:
            # 解码深度有界；更深的输入不能绕过共同底线。
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
