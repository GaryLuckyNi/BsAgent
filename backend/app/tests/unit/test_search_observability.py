"""搜索内部每轮真实 SDK 观测，HTTP 由内存 transport 提供。"""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from app.operations.langfuse_observability import langfuse_agent_run
from app.search.deepseek import DeepSeekWebSearchClient
from app.search.deepseek_api import DeepSeekSearchApiClient
from app.tests.unit.test_langfuse_scheduler import telemetry as scheduler_telemetry

telemetry = scheduler_telemetry


@pytest.mark.parametrize("mode", ["local_content", "metadata_only"])
@pytest.mark.parametrize("status", [200, 429])
def test_direct_search_is_one_api_span_without_generation_or_token_usage(telemetry, mode, status):
    from app.search.base import WebSearchProviderError

    runtime, exporter = telemetry
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"results": [{
            "title": "公告", "url": "https://example.com/source",
            "summary": "synthetic-source-summary", "snippet": "短摘要", "date": None,
        }]})

    async def search():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            return await DeepSeekSearchApiClient(
                api_key="synthetic-direct-secret", http_client=http,
            ).search(query="synthetic-search-query", max_results=3)

    with langfuse_agent_run(business_trace_id=f"direct-search-{uuid4().hex}", session_key="s",
                            tenant_id="t", execution_segment="initial"):
        if status == 200:
            assert asyncio.run(search()).coverage.status == "unknown"
        else:
            with pytest.raises(WebSearchProviderError):
                asyncio.run(search())
    runtime.client.flush()
    runtime.provider.force_flush()
    spans = exporter.get_finished_spans()
    assert not [span for span in spans
                if span.attributes.get("langfuse.observation.type") == "generation"]
    api = [span for span in spans if span.name == "deepseek.search.api"]
    assert len(requests) == len(api) == 1
    span = api[0]
    root = next(row for row in spans if row.name == "bs-agent.run")
    assert span.parent.span_id == root.context.span_id
    assert "langfuse.observation.usage_details" not in span.attributes
    prefix = "langfuse.observation.metadata."
    metadata = {key[len(prefix):]: value for key, value in span.attributes.items()
                if key.startswith(prefix)}
    assert metadata["search_http_status"] == status
    if status == 200:
        assert metadata["search_result_count"] == 1
    else:
        assert metadata["search_error_code"] == "web_search_rate_limited"
    serialized = json.dumps(dict(span.attributes))
    assert "synthetic-direct-secret" not in serialized
    assert ("synthetic-search-query" in serialized) == (mode == "local_content")
    assert ("synthetic-source-summary" in serialized) == (mode == "local_content" and status == 200)


@pytest.mark.parametrize("mode", ["local_content", "metadata_only"])
def test_each_search_turn_has_one_generation_and_accumulated_usage(telemetry, mode):
    runtime, exporter = telemetry
    requests = []
    first = [{"type": "server_tool_use", "name": "web_search", "input": {"query": "PE"}},
             {"type": "web_search_tool_result", "content": [{"title": "资料", "url": "https://example.com/source", "encrypted_content": "private-encrypted-body"}]}]

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "content": first if len(requests) == 1 else [{
                "type": "text", "text": json.dumps({"answer": "仅取得PE", "covered_criteria": [0]}),
            }],
            "stop_reason": "pause_turn" if len(requests) == 1 else "end_turn",
            "usage": {"input_tokens": 10 if len(requests) == 1 else 20,
                      "output_tokens": 3 if len(requests) == 1 else 5},
        })

    async def search():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://model.invalid") as http:
            return await DeepSeekWebSearchClient(api_key="synthetic-private-key", model="test-search", http_client=http).search(
                query="暴跌原因", max_results=3, research_context={
                    "task_goal": "查询PE/PB", "completion_criteria": ["取得PE", "取得PB"],
                    "as_of": "2026-10-23T12:59:00+08:00",
                },
            )

    with langfuse_agent_run(
        business_trace_id=f"search-test-{uuid4().hex}", session_key="s", tenant_id="t",
        execution_segment="initial",
    ):
        result = asyncio.run(search())
    runtime.client.flush()
    spans = exporter.get_finished_spans()
    generations = [span for span in spans if span.attributes.get("langfuse.observation.type") == "generation"]
    assert len(requests) == len(generations) == 2
    assert result.usage == {"input_tokens": 30, "output_tokens": 8}
    assert result.coverage.status == "partial"
    assert requests[1]["messages"][0] == requests[0]["messages"][0]
    root = next(span for span in spans if span.name == "bs-agent.run")
    assert all(span.parent.span_id == root.context.span_id for span in generations)
    assert sum(json.loads(span.attributes["langfuse.observation.usage_details"])["input"] for span in generations) == 30
    for span in generations:
        assert span.name == "deepseek.web_search.turn"
        assert ("langfuse.observation.input" in span.attributes) == (mode != "metadata_only")
        assert ("langfuse.observation.output" in span.attributes) == (mode != "metadata_only")
        serialized = json.dumps(dict(span.attributes))
        assert "synthetic-private-key" not in serialized
        assert "private-encrypted-body" not in serialized
