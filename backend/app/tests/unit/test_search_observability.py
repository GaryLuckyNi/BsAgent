"""搜索内部每轮真实 SDK 观测，HTTP 由内存 transport 提供。"""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from app.operations.langfuse_observability import langfuse_agent_run
from app.search.deepseek import DeepSeekWebSearchClient
from app.tests.unit.test_langfuse_scheduler import telemetry as scheduler_telemetry

telemetry = scheduler_telemetry


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
