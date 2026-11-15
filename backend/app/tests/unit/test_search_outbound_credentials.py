"""两个搜索协议只使用内存 transport，验证实际外发请求次数。"""

import asyncio
import json
from contextlib import contextmanager

import httpx
import pytest
from app.search import deepseek, deepseek_api
from app.security.tool_outbound import ToolOutboundDenied

PAYLOAD = 'Provider: {"api_key":"OUTBOUND_CANARY"}'


@pytest.mark.parametrize("provider", ["api", "legacy"])
@pytest.mark.parametrize("source", ["query", "context"])
def test_search_final_payload_rejects_credentials_before_http_and_observation(
    monkeypatch, provider, source
):
    requests, observations = [], []

    @contextmanager
    def observe(**kwargs):
        observations.append(kwargs)
        yield None

    monkeypatch.setattr(deepseek_api, "langfuse_observation", observe)
    monkeypatch.setattr(deepseek, "langfuse_generation", observe)

    def respond(request):
        requests.append(request)
        if provider == "api":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "公开资料"}]})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://model.invalid"
        ) as http:
            client = (
                deepseek_api.DeepSeekSearchApiClient(api_key="synthetic-auth-key", http_client=http)
                if provider == "api"
                else deepseek.DeepSeekWebSearchClient(
                    api_key="synthetic-auth-key", model="test", http_client=http
                )
            )
            context = (
                {"task_goal": "公开财报", "as_of": "2026-11-02", "original_question": PAYLOAD}
                if source == "context"
                else None
            )
            await client.search(
                query=PAYLOAD if source == "query" else "公开财报",
                max_results=2,
                research_context=context,
            )

    with pytest.raises(ToolOutboundDenied) as caught:
        asyncio.run(run())
    assert "OUTBOUND_CANARY" not in str(caught.value)
    assert requests == []
    assert observations == []


def test_legacy_continuation_does_not_echo_provider_credentials_back_to_model():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "stop_reason": "pause_turn",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "x",
                            "name": "web_search",
                            "input": {"api_key": "OUTBOUND_CANARY"},
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"content": [{"type": "text", "text": "公开资料"}]})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://model.invalid"
        ) as http:
            await deepseek.DeepSeekWebSearchClient(
                api_key="synthetic-auth-key", model="test", http_client=http
            ).search(query="公开财报", max_results=2)

    with pytest.raises(ToolOutboundDenied) as caught:
        asyncio.run(run())
    assert "OUTBOUND_CANARY" not in str(caught.value)
    assert len(requests) == 1


def test_gateway_does_not_claim_entire_search_unstarted_when_continuation_is_blocked():
    from app.schemas.tools import ToolCallRequest
    from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
    from app.tools.registry import ToolRegistry
    from app.tools.web_search import WebSearchTool

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "stop_reason": "pause_turn",
                "content": [
                    {
                        "type": "server_tool_use",
                        "id": "x",
                        "name": "web_search",
                        "input": {"api_key": "OUTBOUND_CANARY"},
                    }
                ],
            },
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://model.invalid"
        ) as http:
            registry = ToolRegistry()
            registry.register(
                WebSearchTool(
                    client=deepseek.DeepSeekWebSearchClient(
                        api_key="synthetic-auth", model="test", http_client=http
                    )
                )
            )
            return await ToolExecutionGateway(registry).execute(
                ToolCallRequest(
                    tool_name="web_search", arguments={"query": "公开财报"}, purpose="测试"
                ),
                trace_id="t",
                context=ToolExecutionContext(
                    tenant_id="t",
                    user_id="u",
                    session_id="s",
                    allowed_tools=["web_search"],
                    permission_scopes=["web:search"],
                ),
            )

    result = asyncio.run(run())
    assert len(requests) == 1
    assert result.status == "failed" and result.execution_state == "uncertain"
    assert result.error.code == "tool_outbound_denied"
    assert "未执行" not in result.error.message
    assert "OUTBOUND_CANARY" not in result.model_dump_json()


def test_direct_search_gateway_checks_only_context_fields_sent_by_that_protocol():
    from app.schemas.tools import ToolCallRequest
    from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
    from app.tools.registry import ToolRegistry
    from app.tools.web_search import WebSearchTool

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "公开资料",
                        "url": "https://example.test/report",
                        "summary": "公开资料",
                    }
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://model.invalid"
        ) as http:
            registry = ToolRegistry()
            registry.register(
                WebSearchTool(
                    client=deepseek_api.DeepSeekSearchApiClient(
                        api_key="synthetic-auth", http_client=http
                    )
                )
            )
            return await ToolExecutionGateway(registry).execute(
                ToolCallRequest(
                    tool_name="web_search",
                    arguments={"query": "公开财报", "max_results": 1},
                    purpose="测试",
                ),
                trace_id="t",
                context=ToolExecutionContext(
                    tenant_id="t",
                    user_id="u",
                    session_id="s",
                    allowed_tools=["web_search"],
                    permission_scopes=["web:search"],
                    research_context={"task_goal": PAYLOAD, "as_of": "2026-11-02"},
                ),
            )

    result = asyncio.run(run())
    assert result.status == "success"
    assert requests == [{"query": "公开财报", "count": 1, "need_content": False}]
    assert "OUTBOUND_CANARY" not in json.dumps(requests)
