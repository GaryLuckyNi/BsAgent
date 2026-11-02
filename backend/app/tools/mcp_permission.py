"""依据错误码归一化 MCP 失败；不通过自然语言文案猜测权限。"""

import json

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
# 官方 HTTP 文档明确 2002 为权限错误。实际 MCP 回包的 40203 同时用于权限/限频，
# 只能保留“请求拒绝”类别，不能用文案正则把它细分或永久缓存成无权限。
_TUSHARE_CODES = {2002: "provider_permission_denied", 40203: "provider_request_rejected"}
_CODE_FIELDS = ("code", "errorcode", "error_code")


def _decoded(value):
    if isinstance(value, str) and len(value) <= 100_000:
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            pass
    return value


def has_execution_result(value, *, _depth=0):
    """仅检查结构化结果；扫描超界时不能证明未执行。"""
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
            or any(value.get(key) is not None for key in ("data", "result", "results", "output"))):
        return True
    return any(has_execution_result(value.get(key), _depth=_depth + 1)
               for key in ("structured_content", "structuredContent", "text", "content", "error"))


def _tushare_text_code(text):
    """兼容 MCP 将数值码序列化进固定错误前缀的格式，只提取码，不读取错误文案。"""
    text = text.strip()
    for prefix in ("tushare api 错误 [", "tushare api error ["):
        if text.lower().startswith(prefix):
            number, separator, tail = text[len(prefix):].partition("]")
            if separator and tail.startswith(":") and number.isascii() and number.isdecimal():
                return int(number) if len(number) <= 10 else None
    return None


def _error_codes(value, *, error_protocol, _depth=0):
    if _depth >= 12:
        return
    value = _decoded(value)
    if isinstance(value, dict):
        for key in _CODE_FIELDS:
            if key in value:
                yield value[key]
        for key in ("error", "structured_content", "structuredContent", "text", "content"):
            if key in value:
                yield from _error_codes(value[key], error_protocol=error_protocol, _depth=_depth + 1)
    elif isinstance(value, list):
        for item in value[:32]:
            yield from _error_codes(item, error_protocol=error_protocol, _depth=_depth + 1)
    elif isinstance(value, str) and error_protocol == "tushare":
        yield _tushare_text_code(value)


def classify_mcp_failure(payload, *, error_context, pre_execution_semantics, error_protocol="generic"):
    if not error_context or has_execution_result(payload):
        return None
    for provider_code in _error_codes(payload, error_protocol=error_protocol):
        if isinstance(provider_code, bool):
            continue
        if isinstance(provider_code, str):
            provider_code = provider_code.lower()
            if provider_code.isascii() and provider_code.isdecimal() and len(provider_code) <= 10:
                provider_code = int(provider_code)
        code = _ERROR_CODES.get(provider_code) if isinstance(provider_code, str) else None
        if code is None and error_protocol == "tushare" and isinstance(provider_code, int):
            code = _TUSHARE_CODES.get(provider_code)
        if code is None:
            continue
        pre_execution = pre_execution_semantics and code in {
            "provider_permission_denied", "provider_request_rejected",
        }
        return {
            "code": code, "execution_state": "not_started" if pre_execution else "uncertain",
            "source": "mcp_adapter", "rule_id": "mcp-error-code-v2", "provider_code": provider_code,
            "phase": "pre_execution" if pre_execution else "unknown",
            "has_partial_result": False, "denied_scope": "arguments",
        }
    return None


def mcp_failure_envelope(payload, *, error_protocol="generic"):
    if not isinstance(payload, dict) or has_execution_result(payload):
        return False
    codes = [payload[key] for key in _CODE_FIELDS if key in payload]
    return (
        (error_protocol == "tushare" and any(key in payload for key in ("msg", "message"))
         and classify_mcp_failure(payload, error_context=True, pre_execution_semantics=False,
                                  error_protocol=error_protocol) is not None)
        or
        any(isinstance(code, str) and code.lower() in _ERROR_CODES for code in codes)
        or (any(code not in (None, 0, "0") for code in codes)
            and any(key in payload for key in ("msg", "message")) and "data" in payload)
        or payload.get("status") == "failed" or isinstance(payload.get("error"), dict)
    )
