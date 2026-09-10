import asyncio
import json
from collections.abc import Callable
from hashlib import sha256
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from app.graph.planning.contracts import SubTaskModel, TaskResultModel
from app.graph.state import AgentState
from app.core.config import settings
from app.graph.state import merge_disclosed_tool_names
from app.llm.factory import ToolCallingLlmClient
from app.schemas.approvals import PendingToolApproval
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult, ToolSpec
from app.tools.default_registry import get_default_tool_registry
from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.native_calls import create_tool_call_message, create_tool_result_message
from app.tools.redaction import (
    redact_sensitive_data,
    reject_inline_credentials,
    reject_server_injected_arguments,
)
from app.tools.registry import ToolCatalogChangedError, ToolRegistry
from app.tools.search import SearchTool, ToolSearchResponse


BUSINESS_FAILURE_CODES = {
    "invalid_field_request",
    "provider_invalid_request",
    "required_api_permission_missing",
    "required_data_missing",
    "unsupported_capability",
    "tool_argument_validation_failed",
}
TRANSIENT_FAILURE_CODES = {
    "mcp_tool_timeout",
    "provider_rate_limited",
    "provider_timeout",
    "provider_unavailable",
    "provider_upstream_error",
    "rag_index_unavailable",
    "rag_service_timeout",
    "rag_service_unreachable",
    "web_search_timeout",
    "web_search_unavailable",
}


class TransientToolExecutionError(RuntimeError):
    http_status = 503

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("工具依赖暂时不可用。")


def build_tool_agent_node(
    *,
    registry_factory: Callable[[], ToolRegistry] | None = None,
    llm_client: ToolCallingLlmClient | None = None,
) -> Callable[[AgentState], dict[str, Any]]:
    selected_registry_factory = registry_factory or get_default_tool_registry

    def node(state: AgentState) -> dict[str, Any]:
        task = _active_tool_task(state)
        registry = selected_registry_factory()
        _validate_tool_catalog(state, registry)
        if llm_client is None:
            return _failure_update(
                task=task,
                code="tool_calling_llm_required",
                content="当前未配置可生成工具参数的 LLM。",
            )

        entry = registry.get(task.tool_name or "")
        request = _staged_request(state, task)
        new_messages = []
        if request is None:
            ai_message = create_tool_call_message(
                llm_client=llm_client,
                messages=_tool_call_messages(state, task),
                tool_spec=entry.spec,
            )
            request, ai_message = _trusted_request(
                state=state,
                task=task,
                tool_spec=entry.spec,
                ai_message=ai_message,
            )
            new_messages.append(ai_message)
        record = ToolCallRecord(call_id=request.call_id, request=request)

        if _requires_approval(entry.spec) and not _is_approved(state, request):
            return {
                "messages": new_messages,
                "tool_calls": [record],
                "pending_tool_approval": _pending_approval(task, request, entry.spec),
            }

        result = asyncio.run(
            ToolExecutionGateway(registry=registry).execute(
                request,
                trace_id=state["trace_id"],
                context=_execution_context(state, task, request),
            )
        )
        if result.error is not None and (
            result.error.retryable or result.error.code in TRANSIENT_FAILURE_CODES
        ):
            raise TransientToolExecutionError(result.error.code)
        record = record.model_copy(update={"result": result})
        content = _tool_message_content(result)
        tool_message = create_tool_result_message(
            tool_call_id=request.call_id or "",
            tool_name=request.tool_name,
            content=content,
            artifact=result.model_dump(mode="json"),
        )
        task_result = _task_result(task, result, content)
        update = {
            "messages": [*new_messages, tool_message],
            "tool_calls": [record],
            "task_results": [task_result.model_dump(mode="python")],
        }
        _apply_tool_search_result(state, request, result, update)
        return update

    node.tool_registry_factory = selected_registry_factory
    node.is_trusted_tool_agent = True
    return node


def _tool_call_messages(
    state: AgentState,
    task: SubTaskModel,
) -> list[AnyMessage]:
    conversation_messages = [
        message
        for message in state["messages"]
        if not isinstance(message, ToolMessage)
        and not (
            isinstance(message, AIMessage)
            and (
                bool(message.tool_calls)
                or bool(message.additional_kwargs.get("task_role"))
            )
        )
    ]
    dependency_ids = set(task.depends_on)
    dependencies = [
        dict(result)
        for result in state["task_results"]
        if result.get("task_id") in dependency_ids
    ]
    instruction = HumanMessage(
        content=json.dumps(
            {
                "instruction": (
                    "只为当前子任务生成已绑定工具的一次调用参数，"
                    "不回答最终问题，不调用其他工具。"
                ),
                "task_goal": task.task_goal,
                "dependencies": dependencies,
            },
            ensure_ascii=False,
        )
    )
    return [*conversation_messages, instruction]


def _active_tool_task(state: AgentState) -> SubTaskModel:
    raw_task = state.get("active_task")
    task = SubTaskModel.model_validate(raw_task)
    if task.tool_name is None:
        raise ValueError("Tool Agent 只能执行包含 tool_name 的子任务。")
    return task


def _validate_tool_catalog(state: AgentState, registry: ToolRegistry) -> None:
    expected = state.get("tool_catalog_digest")
    if not expected or expected != registry.catalog_digest:
        raise ToolCatalogChangedError()


def _trusted_request(
    *,
    state: AgentState,
    task: SubTaskModel,
    tool_spec: ToolSpec,
    ai_message: AIMessage,
) -> tuple[ToolCallRequest, AIMessage]:
    tool_call = ai_message.tool_calls[0]
    arguments = dict(tool_call.get("args") or {})
    reject_inline_credentials(arguments)
    reject_server_injected_arguments(
        arguments,
        schema=tool_spec.input_schema,
        server_injected_fields=tool_spec.server_injected_fields,
    )
    call_id = _trusted_call_id(state, task)
    trusted_call = {**tool_call, "id": call_id, "name": tool_spec.name}
    request = ToolCallRequest(
        call_id=call_id,
        tool_name=tool_spec.name,
        tool_version=tool_spec.version,
        arguments=arguments,
        purpose=task.task_goal,
        approval_required=_requires_approval(tool_spec),
    )
    return request, ai_message.model_copy(update={"tool_calls": [trusted_call]})


def _trusted_call_id(state: AgentState, task: SubTaskModel) -> str:
    identity = ":".join(
        (
            state["request_id"],
            str(state.get("planner_iteration", 0)),
            task.id,
            task.tool_name or "",
        )
    )
    return f"call_{sha256(identity.encode()).hexdigest()[:32]}"


def _staged_request(
    state: AgentState,
    task: SubTaskModel,
) -> ToolCallRequest | None:
    call_id = _trusted_call_id(state, task)
    return next(
        (
            record.request
            for record in state["tool_calls"]
            if (record.call_id or record.request.call_id) == call_id
            and record.result is None
        ),
        None,
    )


def _execution_context(
    state: AgentState,
    task: SubTaskModel,
    request: ToolCallRequest,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=state["tenant_id"],
        user_id=state["user_id"],
        session_id=state["session_id"],
        allowed_tools=(request.tool_name,),
        permission_scopes=tuple(state["permission_scopes"]),
        approved_tool_calls=tuple(state["approved_tool_calls"]),
        skill_name=task.skill_name,
    )


def _requires_approval(tool_spec: ToolSpec) -> bool:
    return tool_spec.risk_level == "high"


def _is_approved(state: AgentState, request: ToolCallRequest) -> bool:
    fingerprint = tool_call_fingerprint(request)
    return any(
        approval.call_id == request.call_id
        and approval.arguments_hash == fingerprint
        for approval in state["approved_tool_calls"]
    )


def _pending_approval(
    task: SubTaskModel,
    request: ToolCallRequest,
    tool_spec: ToolSpec,
) -> PendingToolApproval:
    return PendingToolApproval(
        task_id=task.id,
        call_id=request.call_id or "",
        step_index=0,
        request_fingerprint=tool_call_fingerprint(request),
        tool_name=request.tool_name,
        tool_version=request.tool_version,
        risk_level=tool_spec.risk_level,
        safe_arguments=redact_sensitive_data(request.arguments),
        purpose=f"[{task.id}] {task.task_goal}",
    )


def _tool_message_content(result: ToolCallResult) -> str:
    if result.status == "success":
        payload = result.result or {}
    else:
        error = result.error
        payload = {
            "status": result.status,
            "error_code": error.code if error else "tool_execution_failed",
            "message": error.message if error else "工具执行失败。",
        }
    return json.dumps(
        redact_sensitive_data(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _task_result(
    task: SubTaskModel,
    result: ToolCallResult,
    content: str,
) -> TaskResultModel:
    if result.status == "success":
        return TaskResultModel(task_id=task.id, status="success", content=content)
    error_code = result.error.code if result.error else "tool_execution_failed"
    status = (
        "business_failure"
        if error_code in BUSINESS_FAILURE_CODES
        else "terminal_failure"
    )
    return TaskResultModel(
        task_id=task.id,
        status=status,
        content=content,
        error_code=error_code,
    )


def _failure_update(
    *,
    task: SubTaskModel,
    code: str,
    content: str,
) -> dict[str, Any]:
    result = TaskResultModel(
        task_id=task.id,
        status="terminal_failure",
        content=content,
        error_code=code,
    )
    return {"task_results": [result.model_dump(mode="python")]}


def _apply_tool_search_result(
    state: AgentState,
    request: ToolCallRequest,
    result: ToolCallResult,
    update: dict[str, Any],
) -> None:
    if request.tool_name != SearchTool.name or result.status != "success":
        return
    response = ToolSearchResponse.model_validate(result.result)
    update["disclosed_tool_names"] = merge_disclosed_tool_names(
        state["disclosed_tool_names"],
        [hit.tool_name for hit in response.hits],
        limit=settings.tool_search_max_disclosed_tools,
    )
    update["tool_search_count"] = state["tool_search_count"] + 1


tool_agent_node = build_tool_agent_node()
