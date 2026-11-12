import asyncio
import json

import httpx
import pytest
from app.schemas.tools import ToolCallRequest
from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
from app.tools.rag_search import (
    RagSearchError,
    RagSearchInput,
    RagSearchTool,
    build_default_rag_search_registration,
)
from app.tools.registry import ToolRegistry
from opentelemetry import context as otel_context
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    TraceState,
    set_span_in_context,
)


def test_rag_search_tool_normalizes_results():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/rag/retrieve"
        return httpx.Response(
            200,
            json={
                "query": {"standalone": "报销制度"},
                "contexts": [
                    {
                        "id": "chunk_1",
                        "text": "差旅报销需要提交发票。",
                        "source": {
                            "sourcePath": "kb://policy",
                            "title": "报销制度",
                        },
                        "score": {"final": 0.93},
                        "metadata": {"department": "finance"},
                    }
                ],
                "metadata": {
                    "contextCount": 1,
                    "durationMs": 12,
                },
            },
        )

    async def run_tool():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://rag"
        ) as client:
            tool = RagSearchTool(client=client)
            return await tool.run(
                RagSearchInput(
                    query="报销制度",
                    tenant_id="tenant_1",
                    user_id="user_1",
                )
            )

    result = asyncio.run(run_tool())

    assert result.status == "success"
    assert result.result["results"][0]["chunk_id"] == "chunk_1"
    assert result.tool_name == "rag_search"


def test_rag_search_tool_supports_rag_microservice_retrieve_contract():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = request.read()
        return httpx.Response(
            200,
            json={
                "requestId": "req_1",
                "query": {
                    "original": "报销制度",
                    "standalone": "报销制度",
                    "rewritten": [],
                    "usedForRetrieval": ["报销制度"],
                },
                "queryUnderstanding": {
                    "strategy": "off",
                    "selectedMessageIndexes": [],
                    "droppedMessageIndexes": [],
                    "reason": "off",
                },
                "contexts": [
                    {
                        "id": "knowledge/finance.md#1",
                        "text": "差旅报销需要提交发票。",
                        "source": {
                            "sourcePath": "knowledge/finance.md",
                            "sourceName": "finance.md",
                            "title": "报销制度",
                            "chunkIndex": 1,
                            "citation": "finance.md · 报销制度 · chunk 1",
                        },
                        "score": {"final": 0.93},
                        "metadata": {"department": "finance"},
                    }
                ],
                "graphEvidence": [],
                "sources": [],
                "metadata": {
                    "durationMs": 12,
                    "contextCount": 1,
                    "graphEvidenceCount": 0,
                    "retrieval": {},
                },
                "warnings": [],
            },
        )

    async def run_tool():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://rag"
        ) as client:
            tool = RagSearchTool(client=client)
            return await tool.run(
                RagSearchInput(
                    query="报销制度",
                    tenant_id="tenant_1",
                    user_id="user_1",
                    top_k=5,
                    filters={"docType": "policy"},
                )
            )

    result = asyncio.run(run_tool())

    assert captured["path"] == "/api/rag/retrieve"
    payload = json.loads(captured["payload"])
    assert payload["query"] == "报销制度"
    assert payload["retrieval"]["topK"] == 5
    assert payload["filters"] == {"docType": "policy"}
    assert result.result["results"][0]["chunk_id"] == "knowledge/finance.md#1"
    assert result.result["results"][0]["document_id"] == "knowledge/finance.md"
    assert result.result["results"][0]["score"] == 0.93


def test_agent_rag_search_propagates_w3c_context_and_safe_business_trace_id():
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["traceparent"] = request.headers.get("traceparent")
        captured["tracestate"] = request.headers.get("tracestate")
        captured["business_trace_id"] = request.headers.get("x-business-trace-id")
        return httpx.Response(
            200,
            json={
                "query": {"standalone": "报销制度"},
                "contexts": [],
                "metadata": {"contextCount": 0, "durationMs": 1},
            },
        )

    async def execute_through_agent_gateway():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://rag"
        ) as client:
            tool, binding = build_default_rag_search_registration(base_url="http://rag")
            tool._client = client
            registry = ToolRegistry()
            registry.register(tool, execution_binding=binding)
            return await ToolExecutionGateway(registry).execute(
                request=ToolCallRequest(
                    tool_name="rag_search",
                    arguments={
                        "query": "报销制度",
                        "tenant_id": "tenant_1",
                        "user_id": "user_1",
                    },
                    purpose="检索制度",
                ),
                trace_id="business_trace_123",
                context=ToolExecutionContext(
                    tenant_id="tenant_1",
                    user_id="user_1",
                    session_id="session_1",
                    allowed_tools=("rag_search",),
                    permission_scopes=("rag:retrieve",),
                    business_trace_id="business_trace_123",
                ),
            )

    span_context = SpanContext(
        trace_id=int("0123456789abcdef0123456789abcdef", 16),
        span_id=int("0123456789abcdef", 16),
        is_remote=False,
        trace_flags=TraceFlags.SAMPLED,
        trace_state=TraceState((("vendor", "value"),)),
    )
    token = otel_context.attach(set_span_in_context(NonRecordingSpan(span_context)))
    try:
        result = asyncio.run(execute_through_agent_gateway())
    finally:
        otel_context.detach(token)

    assert result.status == "success"
    assert captured == {
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        "tracestate": "vendor=value",
        "business_trace_id": "business_trace_123",
    }


def test_rag_search_omits_unsafe_business_trace_id_header(monkeypatch):
    captured: dict[str, str | None] = {}
    monkeypatch.setattr("app.tools.rag_search.inject", lambda carrier: None)

    def handler(request: httpx.Request) -> httpx.Response:
        captured["business_trace_id"] = request.headers.get("x-business-trace-id")
        return httpx.Response(
            200,
            json={
                "query": {"standalone": "报销制度"},
                "contexts": [],
                "metadata": {"contextCount": 0, "durationMs": 1},
            },
        )

    async def run_tool():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://rag"
        ) as client:
            tool = RagSearchTool(client=client)
            return await tool.run_with_context(
                RagSearchInput(
                    query="报销制度",
                    tenant_id="tenant_1",
                    user_id="user_1",
                ),
                context=ToolExecutionContext(
                    tenant_id="tenant_1",
                    user_id="user_1",
                    session_id="session_1",
                    business_trace_id="trace\r\nx-leaked: secret",
                ),
            )

    result = asyncio.run(run_tool())

    assert result.status == "success"
    assert captured["business_trace_id"] is None


def test_rag_search_tool_allows_lightweight_retrieval_options():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "query": {"standalone": "RAG 是什么？"},
                "contexts": [
                    {
                        "id": "knowledge/example.md#1",
                        "text": "RAG 是检索增强生成。",
                        "source": {
                            "sourcePath": "knowledge/example.md",
                            "title": "RAG 入门示例",
                        },
                        "score": {"final": 0.9},
                        "metadata": {},
                    }
                ],
                "metadata": {"contextCount": 1, "durationMs": 10},
            },
        )

    async def run_tool():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://rag"
        ) as client:
            tool = RagSearchTool(client=client)
            return await tool.run(
                RagSearchInput(
                    query="RAG 是什么？",
                    tenant_id="tenant_1",
                    user_id="user_1",
                    top_k=3,
                    retrieval_mode="semantic",
                    rerank=False,
                    query_rewrite=False,
                    graph_rag=False,
                )
            )

    asyncio.run(run_tool())

    assert captured["payload"]["retrieval"] == {
        "topK": 3,
        "contextLimit": 3,
        "hybrid": False,
        "rerank": False,
        "queryRewrite": False,
        "graphRag": False,
    }


def test_rag_search_tool_rejects_response_without_results():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query": {"standalone": "报销制度"},
                "metadata": {"contextCount": 0, "durationMs": 12},
            },
        )

    async def run_tool():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://rag"
        ) as client:
            tool = RagSearchTool(client=client)
            return await tool.run(
                RagSearchInput(
                    query="报销制度",
                    tenant_id="tenant_1",
                    user_id="user_1",
                )
            )

    with pytest.raises(RagSearchError) as caught:
        asyncio.run(run_tool())
    assert caught.value.code == "rag_response_invalid"
    assert caught.value.http_status == 502


def test_rag_search_tool_maps_503_to_structured_exception():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "No Chroma index found."})

    async def run_tool():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://rag"
        ) as client:
            tool = RagSearchTool(client=client)
            return await tool.run(
                RagSearchInput(
                    query="报销制度",
                    tenant_id="tenant_1",
                    user_id="user_1",
                )
            )

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(run_tool())

    assert exc_info.value.code == "rag_index_unavailable"
    assert exc_info.value.http_status == 503


def test_rag_search_tool_maps_502_to_structured_exception():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "reranker failed"})

    async def run_tool():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://rag"
        ) as client:
            tool = RagSearchTool(client=client)
            return await tool.run(
                RagSearchInput(
                    query="报销制度",
                    tenant_id="tenant_1",
                    user_id="user_1",
                )
            )

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(run_tool())

    assert exc_info.value.code == "rag_upstream_error"
    assert exc_info.value.http_status == 502


def test_rag_search_tool_maps_network_timeout_to_structured_exception():
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection timed out")

    async def run_tool():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://rag"
        ) as client:
            tool = RagSearchTool(client=client)
            return await tool.run(
                RagSearchInput(
                    query="报销制度",
                    tenant_id="tenant_1",
                    user_id="user_1",
                )
            )

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(run_tool())

    assert exc_info.value.code == "rag_service_timeout"
    assert exc_info.value.http_status == 504
