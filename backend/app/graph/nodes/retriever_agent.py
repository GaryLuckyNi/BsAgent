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
from app.schemas.tools import ToolCallResult, ToolError
from app.schemas.tools import ToolCallRecord, ToolCallRequest
from app.tools.agent import ToolAgentExecutor
from app.tools.execution_gateway import ToolExecutionContext
from app.tools.rag_search import RagSearchError, RagSearchTool
from app.tools.registry import ToolRegistry
from app.tools.fingerprints import tool_call_fingerprint


def retriever_agent_node(state: AgentState) -> AgentState:
    return state


def build_rag_retriever_node(
    *,
    tool: RagSearchTool,
    top_k: int = 8,
) -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        request = _ready_rag_search_call(state)
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
        result = _with_safe_rag_error(result)
        _apply_rag_result(
            state=state,
            request=request,
            result=result,
        )
        _record_unrecoverable_rag_error(state=state, result=result)
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
        request = _ready_rag_search_call(state)

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
        result = _with_safe_rag_error(result)
        _apply_rag_result(
            state=state,
            request=request,
            result=result,
        )
        _record_unrecoverable_rag_error(state=state, result=result)
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
            call_id=request.call_id,
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
    if (
        result.status != "success"
        and (result.error is None or result.error.code != "rag_no_evidence")
    ):
        state["evidence"] = None
    state["ready_tool_call"] = None


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


def _ready_rag_search_call(state: AgentState) -> ToolCallRequest:
    ready = state.get("ready_tool_call")
    if ready is None:
        raise RagSearchError(
            code="rag_call_not_ready",
            message="RAG tool call has not been prepared by Tool Agent.",
            http_status=409,
        )
    plan = state["plan"] or {}
    requested_tools = plan.get("requested_tools", [])
    if ready.step_index >= len(requested_tools):
        raise RagSearchError(
            code="rag_call_not_ready",
            message="Prepared RAG tool step no longer exists.",
            http_status=409,
        )
    raw_request = requested_tools[ready.step_index]
    request = (
        raw_request
        if isinstance(raw_request, ToolCallRequest)
        else ToolCallRequest.model_validate(raw_request)
    )
    if (
        request.tool_name != RagSearchTool.name
        or request.call_id != ready.call_id
        or tool_call_fingerprint(request) != ready.request_fingerprint
    ):
        raise RagSearchError(
            code="rag_call_not_ready",
            message="Prepared RAG tool call no longer matches the execution plan.",
            http_status=409,
        )
    return request


def _record_unrecoverable_rag_error(
    *,
    state: AgentState,
    result: ToolCallResult,
) -> None:
    if result.status == "success" or result.error is None:
        return
    if result.error.code == "rag_no_evidence":
        return
    state["errors"].append(
        {
            "node": "retriever_agent",
            "message": _safe_rag_error_message(result.error.code),
            "code": result.error.code,
            "http_status": _http_status_for_rag_error(result.error.code),
            "handled": True,
        }
    )


def _safe_rag_error_message(code: str) -> str:
    if code == "rag_no_evidence":
        return "未检索到相关证据。"
    if code == "rag_index_unavailable":
        return "检索服务暂不可用。"
    if code in {
        "tool_not_allowed_by_plan",
        "tool_not_allowed_by_skill",
        "tool_permission_denied",
        "tool_approval_required",
        "tool_trusted_call_id_required",
    }:
        return "检索请求未通过执行策略。"
    return "检索请求执行失败。"


def _with_safe_rag_error(result: ToolCallResult) -> ToolCallResult:
    if result.error is None:
        return result
    return result.model_copy(
        update={
            "error": ToolError(
                code=result.error.code,
                message=_safe_rag_error_message(result.error.code),
            )
        }
    )


def _http_status_for_rag_error(code: str) -> int:
    return {
        "rag_index_unavailable": 503,
        "rag_upstream_error": 502,
        "rag_service_timeout": 504,
        "rag_service_unreachable": 502,
    }.get(code, 500)
