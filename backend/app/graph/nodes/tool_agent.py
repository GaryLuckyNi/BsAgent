import asyncio
from collections.abc import Callable

from app.graph.state import AgentState
from app.schemas.tools import ToolCallRecord, ToolCallRequest
from app.tools.default_registry import get_default_tool_registry
from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
from app.tools.registry import ToolRegistry


RESOLVED_SYMBOL_PLACEHOLDER = "$resolved_symbol"


def build_tool_agent_node(
    *,
    registry_factory: Callable[[], ToolRegistry] | None = None,
) -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        requests = _requested_tool_calls(state)
        if not requests:
            return state

        async def run_tools() -> list[ToolCallRecord]:
            context = _execution_context(state)
            if registry_factory is None:
                return await _execute_requests(
                    requests=requests,
                    registry=get_default_tool_registry(),
                    trace_id=state["trace_id"],
                    context=context,
                )
            registry = registry_factory()
            return await _execute_requests(
                requests=requests,
                registry=registry,
                trace_id=state["trace_id"],
                context=context,
            )

        state["tool_calls"].extend(asyncio.run(run_tools()))
        return state

    return node


async def _execute_requests(
    *,
    requests: list[ToolCallRequest],
    registry: ToolRegistry,
    trace_id: str,
    context: ToolExecutionContext,
) -> list[ToolCallRecord]:
    executor = ToolExecutionGateway(registry=registry)
    records: list[ToolCallRecord] = []
    resolved_symbol: str | None = None
    for request in requests:
        resolved_request = _apply_resolved_symbol(request, resolved_symbol)
        result = await executor.execute(
            resolved_request,
            trace_id=trace_id,
            context=context,
        )
        if not result.trace_id:
            result.trace_id = trace_id
        records.append(ToolCallRecord(request=resolved_request, result=result))
        if result.status == "success" and resolved_request.tool_name.endswith(
            ".stock.resolve_company"
        ):
            resolved_symbol = _symbol_from_result(result.result)
    return records


def _apply_resolved_symbol(
    request: ToolCallRequest,
    resolved_symbol: str | None,
) -> ToolCallRequest:
    if resolved_symbol is None:
        return request
    arguments = {
        key: resolved_symbol if value == RESOLVED_SYMBOL_PLACEHOLDER else value
        for key, value in request.arguments.items()
    }
    if arguments == request.arguments:
        return request
    return request.model_copy(update={"arguments": arguments})


def _symbol_from_result(result: dict | None) -> str | None:
    if not result:
        return None
    symbol = result.get("symbol")
    return symbol if isinstance(symbol, str) and symbol else None


def tool_agent_node(state: AgentState) -> AgentState:
    return build_tool_agent_node()(state)


def _requested_tool_calls(state: AgentState) -> list[ToolCallRequest]:
    plan = state["plan"] or {}
    requested_tools = plan.get("requested_tools", [])
    return [
        parsed_request
        for request in requested_tools
        for parsed_request in [
            request
            if isinstance(request, ToolCallRequest)
            else ToolCallRequest.model_validate(request)
        ]
        if parsed_request.tool_name != "rag_search"
    ]


def _execution_context(state: AgentState) -> ToolExecutionContext:
    plan = state["plan"] or {}
    selected_skill = state["selected_skill"]
    return ToolExecutionContext(
        tenant_id=state["tenant_id"],
        user_id=state["user_id"],
        session_id=state["session_id"],
        allowed_tools=plan.get("tool_policy", {}).get("allowed_tools", []),
        skill_name=selected_skill.get("skill_name") if selected_skill else None,
        skill_allowed_tools=(
            selected_skill.get("allowed_tools", []) if selected_skill is not None else None
        ),
    )
