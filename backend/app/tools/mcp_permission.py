"""MCP 调用边界的保守权限分类；不从 tools/list 推断账号权限。"""

import json
import re
from typing import Any

_PERMISSION_CODES = frozenset({
    "permission_denied", "provider_permission_denied", "forbidden",
    "access_denied", "insufficient_permissions", "unauthorized", "unauthenticated",
})
_DENIAL_PHRASES = (
    "您没有访问该接口的权限",
    "你没有访问该接口的权限",
    "没有访问此接口的权限",
    "无权限访问该接口",
    "permission denied",
    "insufficient permissions",
    "access denied",
    "forbidden",
)
_DENIAL_MESSAGE = re.compile(
    r"^(?:error\s*[:：]\s*)?(?:抱歉[，,]?\s*)?"
    r"(?:您没有访问该接口的权限|你没有访问该接口的权限|没有访问此接口的权限|无权限访问该接口"
    r"|permission denied\b|insufficient permissions\b|access denied\b|forbidden\b)",
    re.IGNORECASE,
)


def remote_permission_denied(value: Any) -> bool:
    """仅由已明确标为错误的 MCP 内容调用；不返回或持久化原始错误。"""
    if isinstance(value, dict):
        code = value.get("code")
        if isinstance(code, str) and code.lower() in _PERMISSION_CODES:
            return True
        return any(
            remote_permission_denied(value.get(key))
            for key in ("error", "message", "msg", "text", "structured_content")
        )
    if isinstance(value, list):
        return any(remote_permission_denied(item) for item in value)
    if not isinstance(value, str):
        return False
    normalized = value.lower()
    if any(phrase in normalized for phrase in _DENIAL_PHRASES):
        return True
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, (dict, list)) and remote_permission_denied(parsed)


def has_execution_result(value: Any) -> bool:
    """错误同时携带正常/部分结果时，不将整次执行认定为权限前置拒绝。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    if isinstance(value, list):
        return any(has_execution_result(item) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get("status") in ("success", "complete", "partial") or value.get("data") is not None:
        return True
    return any(has_execution_result(value.get(key)) for key in ("structured_content", "text"))


def permission_failure_envelope(payload: Any) -> bool:
    """按失败信封语义分类，不按接口名推断权限；正常/部分业务数据不改为拒绝。"""
    if not isinstance(payload, dict) or payload.get("data") is not None:
        return False
    if payload.get("status") in ("success", "complete", "partial"):
        return False
    code = payload.get("code")
    if isinstance(code, str) and code.lower() in _PERMISSION_CODES:
        return True
    error = payload.get("error")
    if isinstance(error, dict) and remote_permission_denied(error):
        return True
    return code not in (None, 0, "0") and "msg" in payload and remote_permission_denied(payload)


def standalone_permission_denied(text: str) -> bool:
    """兼容未设置 isError 的纯文本拒绝；不扫描正常业务正文中的权限关键词。"""
    return bool(_DENIAL_MESSAGE.match(text.strip()))
