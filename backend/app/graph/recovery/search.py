"""恢复检索的确定性参数与固定公开失败说明。"""

import json

from app.graph.recovery.batching import LOCAL_BLOCKED_SOURCE_CONSTRAINT
from app.search.base import WEB_SEARCH_ERROR_MESSAGES
from app.tools.redaction import sanitize_untrusted_details

QUERY_FAILURE_MESSAGES = {
    "recovery_query_too_long": "原数据源权限不足；研究目标超出联网搜索词长度限制，未发起替代搜索，原接口数据仍缺失。",
    "recovery_query_invalid": "原数据源权限不足；无法构造符合接口要求的联网搜索参数，未发起替代搜索，原接口数据仍缺失。",
    "recovery_query_redaction_failed": "原数据源权限不足；搜索内容未能完成安全处理，未发起替代搜索，原接口数据仍缺失。",
}


def recovery_search_arguments(task_goal: str) -> dict[str, str]:
    safe = sanitize_untrusted_details({"goal": task_goal})
    if not isinstance(safe.get("goal"), str):
        raise TypeError("recovery_query_redaction_failed")
    # 完整验收条件仍由可信研究上下文绑定，不复制进受长度限制的查询词。
    return {"query": safe["goal"].strip()}


def recovery_failure_message(*, error_code: str | None, content: str) -> str | None:
    if error_code in QUERY_FAILURE_MESSAGES:
        return QUERY_FAILURE_MESSAGES[error_code]
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("original_call_id") or not payload.get("source_notice"):
        return None
    source_constraint = payload.get("source_constraint")
    if source_constraint == LOCAL_BLOCKED_SOURCE_CONSTRAINT:
        original = "历史工具执行尚未核实，本次原接口未派发"
    elif source_constraint:
        original = "原数据源权限不足"
    else:
        # 历史恢复正文没有该字段，不能据此推断账号权限或历史执行状态。
        original = "原接口数据未获取"
    if error_code == "permission_recovery_declined":
        return f"{original}；已选择仅使用已有资料，未发起联网搜索，原接口数据仍缺失。"
    if not error_code:
        return None
    detail = WEB_SEARCH_ERROR_MESSAGES.get(error_code, "补充公开资料的搜索未完成。")
    if error_code == "tool_argument_validation_failed":
        detail = "联网搜索参数不符合接口要求，未实际发起搜索。"
    return f"{original}；{detail}替代搜索未完成，原接口数据仍缺失。"
