import re
from typing import Any


SENSITIVE_KEY_NAMES = {
    "apikey",
    "authorization",
    "accesstoken",
    "bearertoken",
    "clientsecret",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
}


def redact_sensitive_data(
    value: Any,
    *,
    schema: dict[str, Any] | None = None,
    replacement: str = "***",
) -> Any:
    root_schema = schema or {}
    return _redact(
        value,
        schema=root_schema,
        root_schema=root_schema,
        replacement=replacement,
    )


def _redact(
    value: Any,
    *,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    replacement: str,
) -> Any:
    resolved_schema = _resolve_schema(schema, root_schema)
    if _schema_marks_sensitive(resolved_schema):
        return replacement
    if isinstance(value, dict):
        properties = resolved_schema.get("properties", {})
        return {
            key: replacement
            if _is_sensitive_key(str(key))
            else _redact(
                nested,
                schema=properties.get(key, {}),
                root_schema=root_schema,
                replacement=replacement,
            )
            for key, nested in value.items()
        }
    if isinstance(value, list):
        item_schema = resolved_schema.get("items", {})
        return [
            _redact(
                item,
                schema=item_schema,
                root_schema=root_schema,
                replacement=replacement,
            )
            for item in value
        ]
    return value


def _resolve_schema(
    schema: dict[str, Any],
    root_schema: dict[str, Any],
) -> dict[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return schema
    resolved: Any = root_schema
    for segment in reference[2:].split("/"):
        if not isinstance(resolved, dict):
            return schema
        resolved = resolved.get(segment.replace("~1", "/").replace("~0", "~"))
    return resolved if isinstance(resolved, dict) else schema


def _schema_marks_sensitive(schema: dict[str, Any]) -> bool:
    return schema.get("writeOnly") is True or schema.get("x-sensitive") is True


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in SENSITIVE_KEY_NAMES
