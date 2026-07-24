import asyncio
from collections.abc import Callable

from app.graph.state import AgentState
from app.graph.tool_context import build_tool_execution_context
from app.schemas.tools import ToolCallRecord, ToolCallRequest
from app.tools.default_registry import get_default_tool_registry
from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.redaction import redact_sensitive_data
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
            return state

        async def run_tools() -> tuple[list[ToolCallRequest], list[ToolCallRecord]]:
            context = build_tool_execution_context(state)
            return await _advance_requests(
                requests=requests,
                registry=registry,
                trace_id=state["trace_id"],
                context=context,
                existing_records=state["tool_calls"],
            )

        staged_requests, new_records = asyncio.run(run_tools())
        plan = state["plan"] or {}
        state["plan"] = {
            **plan,
            "requested_tools": _merge_staged_requests(
                plan.get("requested_tools", []),
                staged_requests,
            ),
        }
        state["tool_calls"].extend(new_records)
        return state

    return node


async def _advance_requests(
    *,
    requests: list[ToolCallRequest],
    registry: ToolRegistry,
    trace_id: str,
    context: ToolExecutionContext,
    existing_records: list[ToolCallRecord],
) -> tuple[list[ToolCallRequest], list[ToolCallRecord]]:
    executor = ToolExecutionGateway(registry=registry)
    staged_requests = list(requests)
    records: list[ToolCallRecord] = []
    resolution_values = _resolution_values(existing_records)
    completed_hashes = {
        tool_call_fingerprint(record.request)
        for record in existing_records
        if record.result is not None and record.result.status == "success"
    }
    approved_hashes = set(context.approved_tool_call_hashes)
    for index, request in enumerate(requests):
        resolved_request = _resolve_request_placeholders(request, resolution_values)
        resolved_request = _normalize_request_risk(resolved_request, registry)
        staged_requests[index] = resolved_request
        request_hash = tool_call_fingerprint(resolved_request)
        if request_hash in completed_hashes:
            continue
        if _contains_unresolved_placeholder(resolved_request.arguments):
            break
        if resolved_request.approval_required and request_hash not in approved_hashes:
            break
        result = await executor.execute(
            resolved_request,
            trace_id=trace_id,
            context=context,
        )
        if not result.trace_id:
            result.trace_id = trace_id
        records.append(ToolCallRecord(request=resolved_request, result=result))
        if result.status != "success":
            break
        resolution_values.update(result.result or {})
        completed_hashes.add(request_hash)
    return staged_requests, records


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
    return [
        _parse_request(request)
        for request in requested_tools
        if _parse_request(request).tool_name != "rag_search"
    ]


def _merge_staged_requests(
    requested_tools: list,
    staged_requests: list[ToolCallRequest],
) -> list:
    staged = iter(staged_requests)
    return [
        request if _parse_request(request).tool_name == "rag_search" else next(staged)
        for request in requested_tools
    ]


def _normalize_request_risk(
    request: ToolCallRequest,
    registry: ToolRegistry,
) -> ToolCallRequest:
    if not registry.has(request.tool_name):
        return request.model_copy(
            update={"safe_arguments": redact_sensitive_data(request.arguments)}
        )
    try:
        registered_tool = registry.get(request.tool_name, request.tool_version)
    except ValueError:
        return request.model_copy(
            update={"safe_arguments": redact_sensitive_data(request.arguments)}
        )
    updates = {
        "safe_arguments": redact_sensitive_data(
            request.arguments,
            schema=registered_tool.spec.input_schema,
        )
    }
    if registered_tool.spec.risk_level == "high":
        updates["approval_required"] = True
    return request.model_copy(update=updates)


def _parse_request(request) -> ToolCallRequest:
    return (
        request
        if isinstance(request, ToolCallRequest)
        else ToolCallRequest.model_validate(request)
    )
