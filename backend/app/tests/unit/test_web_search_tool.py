import asyncio

import pytest
from app.search.deepseek import WebSearchProviderError
from app.search.schemas import WebSearchResponse, WebSearchResult
from app.tools.web_search import WebSearchInput, WebSearchTool
from pydantic import ValidationError


class StubWebSearchClient:
    def __init__(self, response: WebSearchResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def search(self, **kwargs) -> WebSearchResponse:
        self.calls.append(kwargs)
        return self.response


def test_web_search_tool_returns_bounded_source_evidence():
    client = StubWebSearchClient(
        WebSearchResponse(
            query="长鑫存储 上市主体 股票代码",
            answer="长鑫存储与长鑫科技存在明确主体关系。",
            results=[
                WebSearchResult(
                    title="长鑫科技首次公开发行公告",
                    url="https://www.sse.com.cn/disclosure/example",
                    snippet="A" * 4_000,
                    published_at="2026-05-27",
                )
            ],
            usage={"input_tokens": 20, "output_tokens": 10},
        )
    )

    result = asyncio.run(
        WebSearchTool(client=client).run(
            WebSearchInput(
                query="长鑫存储 上市主体 股票代码",
                max_results=3,
            )
        )
    )

    assert client.calls == [{"query": "长鑫存储 上市主体 股票代码", "max_results": 3}]
    assert result.status == "success"
    assert result.result["query"] == "长鑫存储 上市主体 股票代码"
    assert result.result["results"][0]["url"] == ("https://www.sse.com.cn/disclosure/example")
    assert len(result.result["results"][0]["snippet"]) == 4_000
    assert "usage" not in result.result


def test_gateway_binds_trusted_research_context_without_rewriting_tool_arguments():
    from app.schemas.tools import ToolCallRequest
    from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
    from app.tools.registry import ToolRegistry

    client = StubWebSearchClient(WebSearchResponse(
        query="暴跌原因", answer="未取得估值", results=[
            WebSearchResult(title="资料", url="https://example.com/source"),
        ],
    ))
    registry = ToolRegistry()
    registry.register(WebSearchTool(client=client))
    arguments = {"query": "暴跌原因", "max_results": 3}
    result = asyncio.run(ToolExecutionGateway(registry).execute(
        ToolCallRequest(tool_name="web_search", arguments=arguments, purpose="查询估值"),
        trace_id="trace", context=ToolExecutionContext(
            tenant_id="t", user_id="u", session_id="s", allowed_tools=("web_search",),
            permission_scopes=("web:search",), research_context={
                "task_goal": "查询PE/PB及历史分位", "completion_criteria": ["提供PE和PB"],
                "as_of": "2026-10-23T12:59:00+08:00",
            },
        ),
    ))
    assert result.status == "success"
    assert client.calls[0]["research_context"].task_goal == "查询PE/PB及历史分位"
    assert arguments == {"query": "暴跌原因", "max_results": 3}


def test_web_search_tool_is_discoverable_for_current_market_event_research():
    assert "当日市场涨跌原因" in WebSearchTool.description
    assert "政策" in WebSearchTool.description


def test_web_search_tool_maps_provider_failure_without_raw_diagnostics():
    class FailingClient:
        async def search(self, **_):
            raise WebSearchProviderError(
                code="web_search_timeout",
                message="Authorization: Bearer secret-token",
                retryable=True,
            )

    result = asyncio.run(
        WebSearchTool(client=FailingClient()).run(WebSearchInput(query="长鑫存储", max_results=3))
    )

    assert result.status == "failed"
    assert result.error.code == "web_search_timeout"
    assert result.error.message == "联网搜索服务响应超时。"
    assert result.error.retryable is True
    assert "secret-token" not in result.model_dump_json()


@pytest.mark.parametrize("query", ["", "a" * 501])
def test_web_search_tool_rejects_invalid_query(query):
    with pytest.raises(ValidationError):
        WebSearchInput(query=query, max_results=3)
