import asyncio
from collections.abc import Callable
from uuid import uuid4

import httpx

from app.core.config import settings
from app.graph.state import AgentState
from app.graph.tool_context import (
    DEFAULT_RETRIEVAL_ALLOWED_TOOLS,
    build_tool_execution_context,
)
from app.schemas.evidence import EvidenceItem, EvidencePackage
from app.schemas.tools import ToolCallResult
from app.schemas.tools import ToolCallRecord, ToolCallRequest
from app.tools.agent import ToolAgentExecutor
from app.tools.execution_gateway import ToolExecutionContext
from app.tools.rag_search import RagSearchError, RagSearchInput, RagSearchTool
from app.tools.registry import ToolRegistry


def retriever_agent_node(state: AgentState) -> AgentState:
    return state


def build_rag_retriever_node(
    *,
    tool: RagSearchTool,
    top_k: int = 8,
) -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        request = _requested_rag_search_call(state) or _default_rag_search_request(
            state=state,
            top_k=top_k,
        )
        result = asyncio.run(
            _execute_tool_request(
                request=request,
                tool=tool,
                trace_id=state["trace_id"],
                context=build_tool_execution_context(
                    state,
                    default_allowed_tools=DEFAULT_RETRIEVAL_ALLOWED_TOOLS,
                ),
            )
        )
        _raise_for_unrecoverable_rag_error(result)
        _apply_rag_result(
            state=state,
            request=request,
            result=result,
        )
        return state

    return node


def build_default_rag_retriever_node(
    *,
    base_url: str | None = None,
    top_k: int = 8,
    timeout_seconds: float | None = None,
) -> Callable[[AgentState], AgentState]:
    resolved_base_url = base_url or settings.rag_base_url
    resolved_timeout_seconds = timeout_seconds or settings.rag_timeout_seconds

    def node(state: AgentState) -> AgentState:
        request = _requested_rag_search_call(state) or _default_rag_search_request(
            state=state,
            top_k=top_k,
        )

        async def run_tool() -> ToolCallResult:
            async with httpx.AsyncClient(
                base_url=resolved_base_url,
                timeout=resolved_timeout_seconds,
            ) as client:
                return await _execute_tool_request(
                    request=request,
                    tool=RagSearchTool(client=client),
                    trace_id=state["trace_id"],
                    context=build_tool_execution_context(
                        state,
                        default_allowed_tools=DEFAULT_RETRIEVAL_ALLOWED_TOOLS,
                    ),
                )

        result = asyncio.run(run_tool())
        _raise_for_unrecoverable_rag_error(result)
        _apply_rag_result(
            state=state,
            request=request,
            result=result,
        )
        return state

    return node


def _apply_rag_result(
    *,
    state: AgentState,
    request: ToolCallRequest,
    result: ToolCallResult,
) -> None:
    state["tool_calls"].append(
        ToolCallRecord(
            request=request,
            result=result,
        )
    )

    chunks = result.result["results"] if result.result else []
    state["evidence"] = EvidencePackage(
        evidence_package_id=f"evp_{uuid4().hex}",
        query_set=[result.result.get("query_used", state["user_message"])]
        if result.result
        else [state["user_message"]],
        coverage="sufficient" if chunks else "none",
        items=[
            EvidenceItem(
                evidence_id=f"ev_{index + 1}",
                chunk_id=chunk["chunk_id"],
                document_id=chunk["document_id"],
                source_uri=chunk.get("source_uri"),
                title=chunk.get("title"),
                content=chunk["content"],
                score=chunk["score"],
                metadata=chunk.get("metadata", {}),
            )
            for index, chunk in enumerate(chunks)
        ],
    )


async def _execute_tool_request(
    *,
    request: ToolCallRequest,
    tool: RagSearchTool,
    trace_id: str,
    context: ToolExecutionContext,
) -> ToolCallResult:
    registry = ToolRegistry()
    registry.register(tool)
    return await ToolAgentExecutor(registry=registry).execute(
        request,
        trace_id=trace_id,
        context=context,
    )


def _requested_rag_search_call(state: AgentState) -> ToolCallRequest | None:
    plan = state["plan"] or {}
    requested_tools = plan.get("requested_tools", [])
    for request in requested_tools:
        parsed_request = (
            request
            if isinstance(request, ToolCallRequest)
            else ToolCallRequest.model_validate(request)
        )
        if parsed_request.tool_name == RagSearchTool.name:
            return parsed_request
    return None


def _default_rag_search_request(
    *,
    state: AgentState,
    top_k: int,
) -> ToolCallRequest:
    input_data = RagSearchInput(
        query=state["user_message"],
        tenant_id=state["tenant_id"],
        user_id=state["user_id"],
        top_k=top_k,
        retrieval_mode="hybrid",
    )
    return ToolCallRequest(
        tool_name=RagSearchTool.name,
        tool_version=RagSearchTool.version,
        arguments=input_data.model_dump(),
        purpose="retrieve_policy_evidence",
    )


def _raise_for_unrecoverable_rag_error(result: ToolCallResult) -> None:
    if result.status == "success" or result.error is None:
        return
    if result.error.code == "rag_no_evidence":
        return
    raise RagSearchError(
        code=result.error.code,
        message=result.error.message,
        http_status=_http_status_for_rag_error(result.error.code),
    )


def _http_status_for_rag_error(code: str) -> int:
    return {
        "rag_index_unavailable": 503,
        "rag_upstream_error": 502,
        "rag_service_timeout": 504,
        "rag_service_unreachable": 502,
    }.get(code, 500)
