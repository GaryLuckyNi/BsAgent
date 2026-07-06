import asyncio

import pytest
import httpx

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


def test_deepseek_client_requires_api_key():
    with pytest.raises(LlmConfigurationError) as exc_info:
        DeepSeekClient(api_key="", model="deepseek-v4-flash")

    assert "api key" in str(exc_info.value).lower()
