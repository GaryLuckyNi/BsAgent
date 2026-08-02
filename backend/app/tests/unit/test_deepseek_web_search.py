import asyncio
import json

import httpx
import pytest

from app.search.deepseek import DeepSeekWebSearchClient, WebSearchProviderError


def test_deepseek_web_search_uses_anthropic_server_tool_and_normalizes_sources():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "id": "msg_search_1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "server_tool_use",
                        "id": "srvtoolu_1",
                        "name": "web_search",
                        "input": {"query": "长鑫存储 上市主体 股票代码"},
                    },
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "srvtoolu_1",
                        "content": [
                            {
                                "type": "web_search_result",
                                "url": "https://www.sse.com.cn/disclosure/example",
                                "title": "长鑫科技首次公开发行公告",
                                "page_age": "2026-05-27",
                                "encrypted_content": "opaque",
                            },
                            {
                                "type": "web_search_result",
                                "url": "https://www.example.com/article",
                                "title": "长鑫存储与长鑫科技的主体关系",
                            },
                        ],
                    },
                    {
                        "type": "text",
                        "text": "长鑫科技的证券代码为 688825。",
                        "citations": [
                            {
                                "type": "web_search_result_location",
                                "url": "https://www.sse.com.cn/disclosure/example",
                                "title": "长鑫科技首次公开发行公告",
                            }
                        ],
                    },
                ],
                "usage": {"input_tokens": 100, "output_tokens": 40},
            },
        )

    async def run_search():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.deepseek.com",
        ) as http_client:
            client = DeepSeekWebSearchClient(
                api_key="test-key",
                model="deepseek-v4-flash",
                http_client=http_client,
            )
            return await client.search(
                query="长鑫存储 上市主体 股票代码",
                max_results=5,
            )

    result = asyncio.run(run_search())

    assert captured["path"] == "/anthropic/v1/messages"
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["payload"]["tools"] == [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 1,
        }
    ]
    assert captured["payload"]["messages"][0]["content"] == (
        "长鑫存储 上市主体 股票代码"
    )
    assert captured["payload"]["max_tokens"] <= 768
    assert "candidate_query" in captured["payload"]["system"]
    assert "source_urls" in captured["payload"]["system"]
    assert result.query == "长鑫存储 上市主体 股票代码"
    assert result.answer == "长鑫科技的证券代码为 688825。"
    assert [str(item.url) for item in result.results] == [
        "https://www.sse.com.cn/disclosure/example",
        "https://www.example.com/article",
    ]
    assert result.results[0].published_at == "2026-05-27"
    assert result.usage == {"input_tokens": 100, "output_tokens": 40}


def test_deepseek_web_search_rejects_response_without_search_sources():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "无法确认。"}],
                "usage": {},
            },
        )

    async def run_search():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.deepseek.com",
        ) as http_client:
            return await DeepSeekWebSearchClient(
                api_key="test-key",
                model="deepseek-v4-flash",
                http_client=http_client,
            ).search(query="不存在的公司 股票代码", max_results=3)

    with pytest.raises(WebSearchProviderError) as exc_info:
        asyncio.run(run_search())

    assert exc_info.value.code == "web_search_no_results"
    assert exc_info.value.retryable is False


def test_deepseek_web_search_continues_paused_server_tool_turn():
    requests: list[dict] = []
    search_content = [
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "web_search",
            "input": {"query": "长鑫存储 上市主体 股票代码"},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": [
                {
                    "type": "web_search_result",
                    "url": "https://www.sse.com.cn/disclosure/longxin",
                    "title": "长鑫科技首次公开发行公告",
                }
            ],
        },
    ]
    final_answer = json.dumps(
        {
            "status": "resolved",
            "candidate_query": "长鑫科技",
            "canonical_name": "长鑫科技",
            "symbol": "688825.SH",
            "relation_type": "listed_subject",
            "confidence": 0.96,
            "source_urls": ["https://www.sse.com.cn/disclosure/longxin"],
            "reason": "发行文件支持",
        },
        ensure_ascii=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.read()))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "content": search_content,
                    "stop_reason": "pause_turn",
                    "usage": {"input_tokens": 20, "output_tokens": 10},
                },
            )
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": final_answer}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 40, "output_tokens": 30},
            },
        )

    async def run_search():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.deepseek.com",
        ) as http_client:
            return await DeepSeekWebSearchClient(
                api_key="test-key",
                model="deepseek-v4-flash",
                http_client=http_client,
            ).search(query="长鑫存储 上市主体 股票代码", max_results=5)

    result = asyncio.run(run_search())

    assert len(requests) == 2
    assert requests[1]["messages"] == [
        {"role": "user", "content": "长鑫存储 上市主体 股票代码"},
        {"role": "assistant", "content": search_content},
    ]
    assert requests[0]["max_tokens"] == 768
    assert requests[1]["max_tokens"] == 4_096
    assert requests[1]["output_config"] == {"effort": "low"}
    assert "source_urls 最多 2 个" in requests[1]["system"]
    assert "reason 不超过 40 个中文字符" in requests[1]["system"]
    assert result.answer == final_answer
    assert [str(item.url) for item in result.results] == [
        "https://www.sse.com.cn/disclosure/longxin"
    ]


def test_deepseek_web_search_maps_timeout_to_stable_retryable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timeout", request=request)

    async def run_search():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.deepseek.com",
        ) as http_client:
            return await DeepSeekWebSearchClient(
                api_key="test-key",
                model="deepseek-v4-flash",
                http_client=http_client,
            ).search(query="长鑫存储", max_results=3)

    with pytest.raises(WebSearchProviderError) as exc_info:
        asyncio.run(run_search())

    assert exc_info.value.code == "web_search_timeout"
    assert exc_info.value.retryable is True
