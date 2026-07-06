import asyncio
import os

import httpx
import pytest

from app.core.config import settings
from app.tools.rag_search import RagSearchInput, RagSearchTool


pytestmark = pytest.mark.skipif(
    os.getenv("BS_AGENT_RUN_LIVE_RAG_TESTS") != "true",
    reason="需要显式设置 BS_AGENT_RUN_LIVE_RAG_TESTS=true 才访问真实 RAG 微服务。",
)


def test_live_rag_service_retrieve_contract_is_compatible():
    async def run_tool():
        async with httpx.AsyncClient(base_url=settings.rag_base_url, timeout=60.0) as client:
            tool = RagSearchTool(client=client)
            return await tool.run(
                RagSearchInput(
                    query="RAG 是什么？",
                    tenant_id="live_tenant",
                    user_id="live_user",
                    top_k=3,
                    retrieval_mode="semantic",
                    rerank=False,
                    query_rewrite=False,
                    graph_rag=False,
                )
            )

    result = asyncio.run(run_tool())

    assert result.tool_name == "rag_search"
    assert result.status == "success"
    assert result.result["results"]
