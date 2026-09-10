import asyncio
import json

import pytest
import httpx

from app.llm import deepseek as deepseek_module
from app.llm.deepseek import DeepSeekClient, LlmConfigurationError


def test_deepseek_client_sends_openai_compatible_chat_completion_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["path"] = request.url.path
        captured["payload"] = request.read()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "pong",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            },
        )

    async def run_completion():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.deepseek.com",
        ) as http_client:
            client = DeepSeekClient(
                api_key="test_key",
                model="deepseek-v4-flash",
                http_client=http_client,
            )

            return await client.complete(
                system_prompt="只返回 pong。",
                user_prompt="ping",
                thinking=False,
            )

    result = asyncio.run(run_completion())

    assert result.content == "pong"
    assert result.model == "deepseek-v4-flash"
    assert result.usage == {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "total_tokens": 4,
    }
    assert captured["authorization"] == "Bearer test_key"
    assert captured["path"] == "/chat/completions"
    assert b'"model":"deepseek-v4-flash"' in captured["payload"]
    assert b'"content":"ping"' in captured["payload"]
    assert b'"thinking":{"type":"disabled"}' in captured["payload"]


def test_deepseek_client_requires_api_key():
    with pytest.raises(LlmConfigurationError) as exc_info:
        DeepSeekClient(api_key="", model="deepseek-v4-flash")

    assert "api key" in str(exc_info.value).lower()


def test_deepseek_tool_calling_explicitly_disables_thinking(monkeypatch):
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def bind_tools(self, tools, *, tool_choice):
            return tools, tool_choice

    monkeypatch.setattr(deepseek_module, "ChatOpenAI", FakeChatOpenAI)

    DeepSeekClient(api_key="test_key", model="deepseek-v4-flash")

    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_deepseek_client_streams_openai_compatible_chat_completion_tokens():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["path"] = request.url.path
        payload = json.loads(request.read())
        captured["stream"] = payload["stream"]
        body = (
            'data: {"choices":[{"delta":{"content":"我先"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"分析"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    async def run_stream():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.deepseek.com",
        ) as http_client:
            client = DeepSeekClient(
                api_key="test_key",
                model="deepseek-v4-flash",
                http_client=http_client,
            )
            chunks = []
            async for chunk in client.stream_complete(
                system_prompt="流式输出。",
                user_prompt="开始",
            ):
                chunks.append(chunk)
            return chunks

    chunks = asyncio.run(run_stream())

    assert chunks == ["我先", "分析"]
    assert captured["authorization"] == "Bearer test_key"
    assert captured["path"] == "/chat/completions"
    assert captured["stream"] is True
