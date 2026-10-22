"""MCP 调用边界的保守权限分类；不从 tools/list 推断账号权限。"""

import json
from typing import Any

# 本地核实的纯查询接口，不能由远端 metadata、provider 名称或错误文本扩充。
# income_vip 只读取利润表；未知接口即使权限被拒，也仍可能已经产生写入。
VERIFIED_READ_ONLY_MCP_TOOLS = frozenset({("tushare", "mcp.tushare.income_vip", "income_vip")})
_PERMISSION_CODES = frozenset({"permission_denied", "provider_permission_denied", "forbidden"})
_DENIAL_PHRASES = (
    "您没有访问该接口的权限",
    "你没有访问该接口的权限",
    "没有访问此接口的权限",
    "无权限访问该接口",
    "permission denied",
    "insufficient permissions",
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


def permission_failure_envelope(payload: Any) -> bool:
    """识别 TuShare 原生失败信封，普通业务对象中的 error 字段不构成失败。"""
    return (
        isinstance(payload, dict)
        and payload.get("code") not in (None, 0, "0")
        and "msg" in payload
        and payload.get("data") is None
        and remote_permission_denied(payload)
    )
