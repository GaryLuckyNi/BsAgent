"""将权限失败与本次选择的真实搜索候选绑定，不重新规划或重放原调用。"""

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage

from app.graph.planning.contracts import (
    MAX_TASK_RESULT_CONTENT_CHARS,
    SubTaskModel,
    TaskResultModel,
    task_snapshot_matches,
)
from app.graph.planning.evidence_projection import bounded_search_coverage
from app.graph.recovery.source_policy import resolve_source_policy
from app.graph.state import AgentState
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult, ToolError, ToolSpec
from app.schemas.user_input import UserInputOption, UserInputRequest, UserInputSource
from app.search.schemas import SearchResearchContext
from app.tools.call_budget import SearchCallBudget
from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.native_calls import create_tool_result_message
from app.tools.redaction import redact_sensitive_data, sanitize_untrusted_details
from app.tools.registry import ToolRegistry

PERMISSION_FALLBACK_KIND = "provider_permission_fallback"
PERMISSION_CODES = frozenset(
    {
        "provider_permission_denied",
        "required_api_permission_missing",
        "tool_permission_denied",
    }
)
SOURCE_NOTICE = "联网搜索为替代来源，未经原接口验证；原接口数据仍缺失。"


def is_provider_permission_failure(request: ToolCallRequest, result: ToolCallResult) -> bool:
    return (
        request.tool_name.startswith("mcp.")
        and result.error is not None
        and result.error.code in PERMISSION_CODES
        and result.status != "success"
        and result.execution_state == "not_started"
        and result.result is None
    )


def selection_context(
    state: AgentState,
    task: SubTaskModel,
    request: ToolCallRequest,
    candidates: Sequence[ToolSpec],
    registry: ToolRegistry,
) -> dict[str, Any]:
    return {
        "request_id": state["request_id"],
        "task": task.model_dump(mode="json"),
        "call_id": request.call_id,
        "request_fingerprint": tool_call_fingerprint(request),
        "source_policy": resolve_source_policy(state, task),
        "dispatch_batch_id": (state.get("active_dispatch_batch") or {}).get("dispatch_batch_id"),
        "as_of": state.get("research_as_of"),
        "fixed_skill": bool(state.get("active_skill_step") or task.skill_name),
        "candidates": [
            {
                "name": spec.name,
                "version": spec.version,
                "binding": registry.get(spec.name, spec.version).execution_binding_digest,
            }
            for spec in candidates
        ],
    }


def permission_request(
    state: AgentState,
    task: SubTaskModel,
    request: ToolCallRequest,
    result: ToolCallResult,
    context: dict[str, Any] | None,
) -> UserInputRequest | None:
    if not is_provider_permission_failure(request, result):
        return None
    if not context or not any(item["name"] == "web_search" for item in context["candidates"]):
        return None
    if context.get("completed"):
        return None
    identity = ":".join(
        (state["request_id"], task.id, request.call_id or "", PERMISSION_FALLBACK_KIND)
    )
    return UserInputRequest(
        interaction_id=f"interaction_{sha256(identity.encode()).hexdigest()[:32]}",
        input_type="selection",
        question=f"由于数据源账号权限不足，调用 {request.tool_name[:180]} 工具失败。是否改用联网搜索补充信息？",
        options=(
            UserInputOption(
                value="web_search",
                label="使用联网搜索",
                description="使用公开信息作为替代来源，未经原接口验证。",
            ),
            UserInputOption(
                value="use_existing",
                label="仅使用已有资料",
                description="不联网搜索，并明确说明原接口数据缺失。",
            ),
        ),
        source=UserInputSource(
            node="tool_agent",
            kind=PERMISSION_FALLBACK_KIND,
            task_id=task.id,
            call_id=request.call_id,
        ),
    )


def pending_permission_request(state: AgentState) -> UserInputRequest | None:
    """由串行 scheduler 投影中断，避免并行 worker 争写单值通道。"""
    completed_tasks = {item["task_id"] for item in state.get("task_results", [])}
    records = {item.request.call_id: item for item in state.get("tool_calls", [])}
    for call_id, context in sorted(state.get("tool_selection_contexts", {}).items()):
        task_data = context.get("task", {})
        if task_data.get("id") in completed_tasks:
            continue
        record = records.get(call_id)
        if record is None or record.result is None:
            continue
        if (
            context.get("request_id") != state["request_id"]
            or context.get("call_id") != call_id
            or context.get("request_fingerprint") != tool_call_fingerprint(record.request)
        ):
            continue
        task = SubTaskModel.model_validate(task_data)
        request = permission_request(state, task, record.request, record.result, context)
        if request is not None:
            return request
    return None


def consume_permission_response(
    *,
    state: AgentState,
    task: SubTaskModel,
    registry: ToolRegistry,
    budget: SearchCallBudget,
) -> dict[str, Any] | None:
    response = state.get("user_input_response")
    if response is None or response.source.kind != PERMISSION_FALLBACK_KIND:
        return None
    source = response.source
    contexts = state.get("tool_selection_contexts", {})
    context = contexts.get(source.call_id)
    record = next(
        (
            item
            for item in state.get("tool_calls", [])
            if item.request.call_id == source.call_id and item.call_id == source.call_id
        ),
        None,
    )
    expected = (
        permission_request(state, task, record.request, record.result, context)
        if record and record.result
        else None
    )
    valid = (
        record is not None
        and context is not None
        and (
            source.node == "tool_agent"
            and source.task_id == task.id
            and context.get("request_id") == state["request_id"]
            and context.get("call_id") == source.call_id
            and task_snapshot_matches(context.get("task"), task)
            and context.get("request_fingerprint") == tool_call_fingerprint(record.request)
        )
    )
    if (
        not valid
        or expected is None
        or expected.interaction_id != response.interaction_id
        or expected.source != source
        or response.value not in {"web_search", "use_existing"}
    ):
        return _completed_update(
            task,
            code="permission_fallback_resume_invalid",
            content="权限失败的恢复来源不匹配，未执行搜索；原接口数据仍缺失。",
        )
    completion = {"tool_selection_contexts": {source.call_id: {**context, "completed": True}}}
    if response.value == "use_existing":
        return {
            **_completed_update(
                task,
                code=record.result.error.code,
                content=f"数据源账号权限不足；用户选择仅使用已有资料，{task.task_goal}所需原接口数据缺失。",
            ),
            **completion,
        }
    candidate = next(item for item in context["candidates"] if item["name"] == "web_search")
    if not registry.has("web_search"):
        return {
            **_completed_update(
                task,
                code="tool_not_found",
                content="原候选中的联网搜索工具已不可用，原接口数据仍缺失。",
            ),
            **completion,
        }
    entry = registry.get("web_search")
    if (
        entry.spec.version != candidate["version"]
        or entry.execution_binding_digest != candidate["binding"]
    ):
        return {
            **_completed_update(
                task,
                code="permission_fallback_resume_invalid",
                content="联网搜索的原候选绑定已变化，未执行搜索；原接口数据仍缺失。",
            ),
            **completion,
        }
    call_id = (
        f"call_{sha256((source.call_id + ':permission:web_search').encode()).hexdigest()[:32]}"
    )
    # 同时保留实体与本步目标；不把原工具参数、凭证或原始错误发送给搜索。
    query_text = f"{state['user_message'][:200]}\n{task.task_goal[:250]}"
    query = sanitize_untrusted_details({"query": query_text.replace('"', "").replace("'", "")})[
        "query"
    ][:500]
    request = ToolCallRequest(
        call_id=call_id,
        tool_name="web_search",
        tool_version=candidate["version"],
        arguments={"query": query},
        purpose=f"替代来源：{task.task_goal}",
    )
    result = _execute_search(state=state, task=task, request=request, registry=registry, budget=budget)
    content = _search_content(result)
    update = _completed_update(
        task,
        code=result.error.code if result.error else None,
        content=content,
        success=result.status == "success",
        tool_name="web_search",
    )
    update["messages"] = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": call_id,
                    "name": "web_search",
                    "args": request.arguments,
                    "type": "tool_call",
                }
            ],
        ),
        create_tool_result_message(
            tool_call_id=call_id,
            tool_name="web_search",
            content=content,
            artifact=result.model_dump(mode="json"),
        ),
    ]
    update["tool_calls"] = [ToolCallRecord(call_id=call_id, request=request, result=result)]
    return {**update, **completion}


def _execute_search(*, state, task, request, registry, budget) -> ToolCallResult:
    call_id = request.call_id
    existing = next(
        (
            item.result
            for item in state["tool_calls"]
            if item.request.call_id == call_id and item.result is not None
        ),
        None,
    )
    run_key = json.dumps([state["tenant_id"], state["session_id"], state["request_id"]])
    used = tuple(
        item.request.call_id
        for item in state["tool_calls"]
        if item.request.tool_name == "web_search"
        and item.result is not None
        and not (item.result.error and item.result.error.code == "research_budget_exhausted")
        and item.request.call_id
    )
    if existing is not None:
        result = existing
    elif not state.get("web_search_allowed", True) or not budget.reserve(
        run_key, call_id, existing=used
    ):
        result = ToolCallResult(
            tool_call_id=call_id,
            tool_name="web_search",
            status="denied",
            trace_id=state["trace_id"],
            error=ToolError(code="research_budget_exhausted", message="搜索调用预算已耗尽。"),
        )
    else:
        result = asyncio.run(
            ToolExecutionGateway(registry).execute(
                request,
                trace_id=state["trace_id"],
                context=ToolExecutionContext(
                    tenant_id=state["tenant_id"],
                    user_id=state["user_id"],
                    session_id=state["session_id"],
                    business_trace_id=state["trace_id"],
                    allowed_tools=("web_search",),
                    permission_scopes=tuple(state["permission_scopes"]),
                    approved_tool_calls=tuple(state["approved_tool_calls"]),
                    research_context=SearchResearchContext(
                        task_goal=task.task_goal, completion_criteria=tuple(task.completion_criteria),
                        as_of=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
                        original_question=state.get("user_message"),
                    ),
                ),
            )
        )
    return result


def _search_content(result: ToolCallResult) -> str:
    """限制 checkpoint 中的任务正文，同时保留可引用来源和替代说明。"""
    payload = (
        {"source_notice": SOURCE_NOTICE, "search_result": result.result}
        if result.status == "success"
        else {
            "source_notice": SOURCE_NOTICE,
            "error_code": result.error.code if result.error else None,
            "message": result.error.message if result.error else "搜索失败。",
        }
    )
    content = json.dumps(redact_sensitive_data(payload), ensure_ascii=False)
    if len(content) <= MAX_TASK_RESULT_CONTENT_CHARS:
        return content
    if result.status == "success" and isinstance(result.result, dict):
        source = result.result
        summary = {
            "source_notice": SOURCE_NOTICE,
            "search_result": {
                "query": str(source.get("query") or "")[:500],
                "answer": str(source.get("answer") or "")[:2500],
                "results": [],
            },
            "truncated": True,
        }
        coverage = bounded_search_coverage(source.get("coverage"))
        if coverage is not None:
            summary["search_result"]["coverage"] = coverage
        references = source.get("results")
        for item in references[:10] if isinstance(references, list) else []:
            if not isinstance(item, dict):
                continue
            ref = {
                "title": str(item.get("title") or "")[:200],
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("snippet") or "")[:300],
                "published_at": item.get("published_at"),
            }
            rows = summary["search_result"]["results"]
            rows.append(ref)
            if len(json.dumps(summary, ensure_ascii=False)) > MAX_TASK_RESULT_CONTENT_CHARS:
                rows.pop()
        bounded = json.dumps(redact_sensitive_data(summary), ensure_ascii=False)
        if len(bounded) <= MAX_TASK_RESULT_CONTENT_CHARS:
            return bounded
    return json.dumps(
        {"source_notice": SOURCE_NOTICE, "preview": content[:1000], "truncated": True},
        ensure_ascii=False,
    )


def _completed_update(
    task: SubTaskModel, *, code, content, success=False, tool_name=None
) -> dict[str, Any]:
    update = {
        "task_results": [
            TaskResultModel(
                task_id=task.id,
                status="success" if success else "business_failure",
                content=content,
                error_code=code,
                tool_name=tool_name,
            ).model_dump(mode="python")
        ],
        "user_input_response": None,
    }
    if not success:
        update["planner_stop_reason"] = "provider_permission_fallback_complete"
    return update
