"""MCP 调用边界的保守权限分类；不从 tools/list 推断账号权限。"""

import json
import re
from typing import Any

_ERROR_CODES = {
    **dict.fromkeys(("permission_denied", "provider_permission_denied", "forbidden",
                    "access_denied", "insufficient_permissions"), "provider_permission_denied"),
    **dict.fromkeys(("unauthorized", "unauthenticated", "authentication_failed",
                    "provider_authentication_failed"), "provider_authentication_failed"),
    **dict.fromkeys(("rate_limit_exceeded", "too_many_requests", "rate_limited",
                    "provider_rate_limited"), "provider_rate_limited"),
    **dict.fromkeys(("quota_exceeded", "insufficient_quota", "provider_quota_exceeded"),
                   "provider_quota_exceeded"),
    **dict.fromkeys(("invalid_params", "invalid_request", "provider_invalid_request"),
                   "provider_invalid_request"),
}
_DENIAL_MESSAGE = re.compile(
    r"^(?:error\s*[:：]\s*)?(?:抱歉[，,]?\s*)?"
    r"(?:(?:您|你)?没有(?:访问(?:该|此)?接口的权限|(?:该|此)?接口(?:的)?访问权限)"
    r"|无权限访问(?:该|此)?接口|permission denied\b|insufficient permissions\b"
    r"|access denied\b|forbidden\b)",
    re.IGNORECASE,
)


def remote_permission_denied(value: Any) -> bool:
    found = classify_mcp_failure(value, error_context=True, pre_execution_semantics=False)
    return found is not None and found["code"] == "provider_permission_denied"


def has_execution_result(value: Any, *, _depth: int = 0) -> bool:
    """扫描超界时保守视为有结果，不能以扫描不完整证明未执行。"""
    if _depth >= 12:
        return True
    value = _decoded(value)
    if isinstance(value, str):
        return len(value) > 100_000
    if isinstance(value, list):
        return len(value) > 32 or any(has_execution_result(x, _depth=_depth + 1) for x in value)
    if not isinstance(value, dict):
        return False
    if (value.get("status") in ("success", "complete", "completed", "partial")
            or any(value.get(key) is not None for key in ("data", "result", "results"))):
        return True
    return any(has_execution_result(value.get(key), _depth=_depth + 1)
               for key in ("structured_content", "structuredContent", "text", "content", "error"))


def permission_failure_envelope(payload: Any) -> bool:
    """按失败信封语义分类，不按接口名推断权限；正常/部分业务数据不改为拒绝。"""
    payload = _decoded(payload)
    if not isinstance(payload, dict) or has_execution_result(payload):
        return False
    if payload.get("code") in (None, 0, "0") and not isinstance(payload.get("error"), dict):
        return False
    return remote_permission_denied(payload)


def standalone_permission_denied(text: str) -> bool:
    """兼容未设置 isError 的纯文本拒绝；不扫描正常业务正文中的权限关键词。"""
    return bool(_DENIAL_MESSAGE.match(text.strip()))


def _decoded(value: Any) -> Any:
    if isinstance(value, str) and len(value) <= 100_000:
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            pass
    return value


def _error_subjects(value: Any, *, _depth: int = 0):
    if _depth >= 12:
        return
    value = _decoded(value)
    if isinstance(value, str):
        yield None, value
    elif isinstance(value, list):
        for item in value[:32]:
            yield from _error_subjects(item, _depth=_depth + 1)
    elif isinstance(value, dict):
        yield value.get("code"), value.get("message", value.get("msg", ""))
        for key in ("error", "structured_content", "structuredContent", "text", "content"):
            if key in value:
                yield from _error_subjects(value[key], _depth=_depth + 1)


def classify_mcp_failure(payload, *, error_context: bool, pre_execution_semantics: bool):
    if not error_context or has_execution_result(payload):
        return None
    for provider_code, message in _error_subjects(payload):
        code = _ERROR_CODES.get(provider_code.lower()) if isinstance(provider_code, str) else None
        rule = "mcp-error-code-v1"
        if code is None and isinstance(message, str) and standalone_permission_denied(message):
            code, rule = "provider_permission_denied", "mcp-denial-subject-v1"
        if code is None:
            continue
        pre_execution = pre_execution_semantics and code == "provider_permission_denied"
        safe_code = provider_code if isinstance(provider_code, int) and not isinstance(provider_code, bool) else None
        if isinstance(provider_code, str) and provider_code.lower() in _ERROR_CODES:
            safe_code = provider_code.lower()
        return {
            "code": code,
            "execution_state": "not_started" if pre_execution else "uncertain",
            "source": "mcp_adapter", "rule_id": rule, "provider_code": safe_code,
            "phase": "pre_execution" if pre_execution else "unknown",
            "has_partial_result": False, "denied_scope": "arguments",
        }
    return None
