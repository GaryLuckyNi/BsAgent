import asyncio
from collections.abc import Callable
from hashlib import sha256

from app.graph.state import AgentState
from app.graph.tool_context import build_tool_execution_context
from app.schemas.approvals import PendingToolApproval, ReadyToolCall
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolSpec
from app.tools.default_registry import get_default_tool_registry
from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.rag_search import RagSearchInput, RagSearchTool
from app.tools.redaction import (
    redact_sensitive_data,
    reject_inline_credentials,
    reject_server_injected_arguments,
)
from app.tools.registry import ToolRegistry


RESOLVED_VALUE_PREFIX = "$resolved_"


def build_tool_agent_node(
    *,
    registry_factory: Callable[[], ToolRegistry] | None = None,
) -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        registry = (
            registry_factory()
            if registry_factory is not None
            else get_default_tool_registry()
        )
        requests = _requested_tool_calls(state)
        if not requests:
            state["pending_tool_approval"] = None
            state["ready_tool_call"] = None
            return state

        async def run_tools() -> tuple[
            list[ToolCallRequest],
            list[ToolCallRecord],
            PendingToolApproval | None,
            ReadyToolCall | None,
        ]:
            context = build_tool_execution_context(state)
            return await _advance_requests(
                requests=requests,
                registry=registry,
                trace_id=state["trace_id"],
                context=context,
                existing_records=state["tool_calls"],
                run_id=state["request_id"],
            )

        staged_requests, new_records, pending_approval, ready_call = asyncio.run(
            run_tools()
        )
        plan = state["plan"] or {}
        state["plan"] = {
            **plan,
            "requested_tools": _merge_staged_requests(staged_requests),
        }
        state["tool_calls"].extend(new_records)
        state["pending_tool_approval"] = pending_approval
        state["ready_tool_call"] = ready_call
        return state

    return node


async def _advance_requests(
    *,
    requests: list[ToolCallRequest],
    registry: ToolRegistry,
    trace_id: str,
    context: ToolExecutionContext,
    existing_records: list[ToolCallRecord],
    run_id: str,
) -> tuple[
    list[ToolCallRequest],
    list[ToolCallRecord],
    PendingToolApproval | None,
    ReadyToolCall | None,
]:
    executor = ToolExecutionGateway(registry=registry)
    staged_requests = list(requests)
    records: list[ToolCallRecord] = []
    resolution_values = _resolution_values(existing_records)
    completed_call_ids = {
        record.call_id or record.request.call_id
        for record in existing_records
        if record.result is not None and record.result.status == "success"
        and (record.call_id or record.request.call_id) is not None
    }
    approved_calls = {
        (approval.call_id, approval.arguments_hash)
        for approval in context.approved_tool_calls
    }
    for index, request in enumerate(requests):
        reject_inline_credentials(request.arguments)
        request = request.model_copy(
            update={"call_id": _trusted_call_id(run_id=run_id, step_index=index)}
        )
        resolved_request = _resolve_request_placeholders(request, resolution_values)
        resolved_request = _normalize_request_risk(resolved_request, registry)
        tool_spec = _trusted_tool_spec(registry, resolved_request)
        if tool_spec is not None:
            reject_server_injected_arguments(
                resolved_request.arguments,
                schema=tool_spec.input_schema,
                server_injected_fields=tool_spec.server_injected_fields,
            )
        staged_requests[index] = resolved_request
        request_hash = tool_call_fingerprint(resolved_request)
        if resolved_request.call_id in completed_call_ids:
            continue
        if _contains_unresolved_placeholder(resolved_request.arguments):
            break
        if resolved_request.approval_required and (
            resolved_request.call_id,
            request_hash,
        ) not in approved_calls:
            safe_arguments = redact_sensitive_data(
                resolved_request.arguments,
                schema=tool_spec.input_schema if tool_spec else None,
            )
            return (
                staged_requests,
                records,
                PendingToolApproval(
                    call_id=resolved_request.call_id,
                    step_index=index,
                    request_fingerprint=request_hash,
                    tool_name=resolved_request.tool_name,
                    tool_version=resolved_request.tool_version,
                    risk_level="high",
                    safe_arguments=safe_arguments,
                    purpose=resolved_request.purpose,
                ),
                None,
            )
        if resolved_request.tool_name == RagSearchTool.name:
            return (
                staged_requests,
                records,
                None,
                ReadyToolCall(
                    call_id=resolved_request.call_id,
                    step_index=index,
                    request_fingerprint=request_hash,
                    tool_name=resolved_request.tool_name,
                ),
            )
        result = await executor.execute(
            resolved_request,
            trace_id=trace_id,
            context=context,
        )
        if not result.trace_id:
            result.trace_id = trace_id
        records.append(
            ToolCallRecord(
                call_id=resolved_request.call_id,
                request=resolved_request,
                result=result,
            )
        )
        if result.status != "success":
            break
        resolution_values.update(result.result or {})
        completed_call_ids.add(resolved_request.call_id)
    return staged_requests, records, None, None


def _trusted_call_id(*, run_id: str, step_index: int) -> str:
    digest = sha256(f"{run_id}:{step_index}".encode()).hexdigest()
    return f"call_{digest[:24]}"


def _resolve_request_placeholders(
    request: ToolCallRequest,
    resolution_values: dict,
) -> ToolCallRequest:
    arguments = _resolve_value(request.arguments, resolution_values)
    if arguments == request.arguments:
        return request
    return request.model_copy(update={"arguments": arguments})


def _resolve_value(value, resolution_values: dict):
    if isinstance(value, str) and value.startswith(RESOLVED_VALUE_PREFIX):
        output_key = value.removeprefix(RESOLVED_VALUE_PREFIX)
        return resolution_values.get(output_key, value)
    if isinstance(value, dict):
        return {
            key: _resolve_value(nested, resolution_values)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_resolve_value(item, resolution_values) for item in value]
    return value


def _contains_unresolved_placeholder(value) -> bool:
    if isinstance(value, str):
        return value.startswith(RESOLVED_VALUE_PREFIX)
    if isinstance(value, dict):
        return any(_contains_unresolved_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_unresolved_placeholder(item) for item in value)
    return False


def _resolution_values(records: list[ToolCallRecord]) -> dict:
    values = {}
    for record in records:
        if record.result is not None and record.result.status == "success":
            values.update(record.result.result or {})
    return values


def tool_agent_node(state: AgentState) -> AgentState:
    return build_tool_agent_node()(state)


def _requested_tool_calls(state: AgentState) -> list[ToolCallRequest]:
    plan = state["plan"] or {}
    requested_tools = plan.get("requested_tools", [])
    return [_parse_request(request) for request in requested_tools]


def _merge_staged_requests(staged_requests: list[ToolCallRequest]) -> list:
    return list(staged_requests)


def _normalize_request_risk(
    request: ToolCallRequest,
    registry: ToolRegistry,
) -> ToolCallRequest:
    tool_spec = _trusted_tool_spec(registry, request)
    if tool_spec is None:
        return request
    updates = {}
    if tool_spec.risk_level == "high":
        updates["approval_required"] = True
    return request.model_copy(update=updates)


def _trusted_tool_spec(
    registry: ToolRegistry,
    request: ToolCallRequest,
) -> ToolSpec | None:
    if registry.has(request.tool_name):
        try:
            return registry.get(request.tool_name, request.tool_version).spec
        except ValueError:
            return None
    if request.tool_name != RagSearchTool.name:
        return None
    if request.tool_version not in {None, RagSearchTool.version}:
        return None
    return ToolSpec(
        name=RagSearchTool.name,
        version=RagSearchTool.version,
        description=RagSearchTool.description,
        input_schema=RagSearchInput.model_json_schema(),
        permission_scope=RagSearchTool.permission_scope,
        risk_level=RagSearchTool.risk_level,
    )


def _parse_request(request) -> ToolCallRequest:
    return (
        request
        if isinstance(request, ToolCallRequest)
        else ToolCallRequest.model_validate(request)
    )
