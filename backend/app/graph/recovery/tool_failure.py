"""固定 Skill 的显式搜索兜底兼容路径；普通任务使用多来源备用调用链。"""

import asyncio
import json
from hashlib import sha256

from app.graph.nodes.permission_fallback import _search_content
from app.graph.planning.contracts import MAX_TASK_RESULT_CONTENT_CHARS
from app.graph.planning.policy import MAX_WEB_SEARCH_TASKS
from app.graph.planning.skill_workflow import active_step
from app.graph.recovery.search import recovery_search_arguments
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult, ToolError
from app.search.schemas import SearchResearchContext
from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway

PARAMETER_ERROR_CODES = frozenset({
    "invalid_field_request", "provider_invalid_request", "invalid_tool_arguments",
    "tool_argument_validation_failed",
})


def fallback_call_id(original_call_id):
    return "call_" + sha256(f"{original_call_id}:automatic:web_search".encode()).hexdigest()[:32]


def skill_allows_search(workflow, step):
    source = step.web_search_fallback_source
    return bool(source and source in workflow.summary and "web_search" in source)


def automatic_search_fallback(*, state, task, request, result, registry, budget,
                              availability_registry_factory=None):
    if result.status == "success" or result.error is None or result.result is not None:
        return None
    fixed = active_step(state)
    spec = registry.get(request.tool_name, request.tool_version).spec
    if fixed:
        if not skill_allows_search(*fixed):
            return None
        if spec.effect_type != "read_only" and result.execution_state != "not_started":
            return None
    else:
        # 普通取证使用预生成的备用调用链，不硬拼搜索查询。
        return None
    if not registry.has("web_search"):
        return None
    search = registry.get("web_search").spec
    replacement = ToolCallRequest(
        call_id=fallback_call_id(request.call_id), tool_name="web_search", tool_version=search.version,
        arguments=recovery_search_arguments(task.task_goal), purpose="按任务失败策略补充公开资料",
        approval_required=search.risk_level == "high",
    )
    previous = next((row for row in state.get("tool_calls", [])
                     if row.request.call_id == replacement.call_id and row.result is not None), None)
    if previous:
        return previous
    used = [row.request.call_id for row in state.get("tool_calls", [])
            if row.request.tool_name == "web_search" and row.result is not None
            and not (row.result.error and row.result.error.code == "research_budget_exhausted")]
    if not state.get("web_search_allowed", True) or not budget.reserve(
        json.dumps([state["tenant_id"], state["session_id"], state["request_id"]]),
        replacement.call_id, existing=used,
    ):
        search_result = ToolCallResult(
            tool_call_id=replacement.call_id, tool_name="web_search", status="denied",
            execution_state="not_started", trace_id=state["trace_id"],
            error=ToolError(code="research_budget_exhausted", message="搜索调用预算已耗尽。"),
        )
    else:
        search_result = asyncio.run(ToolExecutionGateway(
            registry, availability_registry_factory=availability_registry_factory,
        ).execute(replacement, trace_id=state["trace_id"], context=ToolExecutionContext(
            tenant_id=state["tenant_id"], user_id=state["user_id"], session_id=state["session_id"],
            request_id=state["request_id"], business_trace_id=state["trace_id"],
            allowed_tools=("web_search",), permission_scopes=tuple(state["permission_scopes"]),
            approved_tool_calls=tuple(state["approved_tool_calls"]),
            search_budget_limit=MAX_WEB_SEARCH_TASKS, existing_search_call_ids=tuple(used),
            research_context=SearchResearchContext(
                task_goal=task.task_goal, completion_criteria=tuple(task.completion_criteria),
                as_of=state.get("research_as_of"), original_question=state["user_message"],
            ),
        )))
    return ToolCallRecord(call_id=replacement.call_id, request=replacement, result=search_result)


def fallback_content(original, replacement):
    payload = json.loads(_search_content(replacement.result))
    payload.update(original_tool=original.request.tool_name, original_call_id=original.request.call_id)
    # 先缩减搜索副本，始终保留原失败与替代来源说明；canonical 回执不变。
    while len(json.dumps(payload, ensure_ascii=False)) > MAX_TASK_RESULT_CONTENT_CHARS:
        search_result = payload.get("search_result")
        results = search_result.get("results") if isinstance(search_result, dict) else None
        if isinstance(results, list) and results:
            results.pop()
            payload["truncated"] = True
        else:
            payload.pop("search_result", None)
            payload.pop("preview", None)
            payload["truncated"] = True
            break
    return json.dumps(payload, ensure_ascii=False)
