import asyncio

import pytest
from pydantic import ValidationError

from app.search.deepseek import WebSearchProviderError
from app.search.schemas import WebSearchResponse, WebSearchResult
from app.tools.web_search import WebSearchInput, WebSearchTool


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
