import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.tools.rag_search import RagSearchInput, RagSearchTool


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

    with pytest.raises(ValidationError):
        asyncio.run(run_tool())


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
