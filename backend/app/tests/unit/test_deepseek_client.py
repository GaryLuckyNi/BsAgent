import asyncio
import json
from contextlib import contextmanager
from typing import ClassVar

import httpx
import pytest
from app.llm import deepseek as deepseek_module
from app.llm.deepseek import DeepSeekClient, LlmConfigurationError, LlmProviderError


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


def test_deepseek_callback_completion_enables_thinking_for_graph_stream(monkeypatch):
    captured = {}

    class FakeResponse:
        content = "最终内容"
        usage_metadata: ClassVar = {"output_tokens": 4}

    class FakeBoundModel:
        def invoke(self, messages):
            captured["messages"] = messages
            return FakeResponse()

    class FakeChatOpenAI:
        def __init__(self, **_kwargs):
            pass

        def bind(self, **kwargs):
            captured["bind"] = kwargs
            return FakeBoundModel()

    monkeypatch.setattr(deepseek_module, "ChatOpenAI", FakeChatOpenAI)
    client = DeepSeekClient(api_key="test_key", model="deepseek-v4-flash")

    result = client.complete_with_streaming_callbacks(
        system_prompt="系统",
        user_prompt="用户",
        max_tokens=512,
        thinking=True,
    )

    assert result.content == "最终内容"
    assert captured["bind"]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert captured["bind"]["reasoning_effort"] == "high"
    assert "temperature" not in captured["bind"]


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


def test_deepseek_client_streams_reasoning_and_answer_as_distinct_events():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.read())
        body = (
            'data: {"choices":[{"delta":{"reasoning_content":"先核对公司主体。"}}]}\n\n'
            'data: {"choices":[{"delta":{"reasoning_content":"再分析证据。"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"## 结论"}}]}\n\n'
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
            return [
                event
                async for event in client.stream_complete_events(
                    system_prompt="进行投研分析。",
                    user_prompt="分析一下银之杰",
                    thinking=True,
                )
            ]

    events = asyncio.run(run_stream())

    assert [(event.kind, event.delta) for event in events] == [
        ("reasoning", "先核对公司主体。"),
        ("reasoning", "再分析证据。"),
        ("content", "## 结论"),
    ]
    assert captured["payload"]["thinking"] == {"type": "enabled"}


def test_deepseek_streamed_completion_aggregates_answer_and_forwards_deltas():
    def handler(_request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"delta":{"reasoning_content":"先查找证据。"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"## 结论\\n\\n"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"证据支持该判断。"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
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
            deltas = []
            result = await client.complete_streamed(
                system_prompt="进行投研分析。",
                user_prompt="开始",
                thinking=True,
                on_delta=deltas.append,
            )
            return result, deltas

    result, deltas = asyncio.run(run_completion())

    assert result.content == "## 结论\n\n证据支持该判断。"
    assert result.reasoning_content == "先查找证据。"
    assert [(delta.kind, delta.delta) for delta in deltas] == [
        ("reasoning", "先查找证据。"),
        ("content", "## 结论\n\n"),
        ("content", "证据支持该判断。"),
    ]


def test_deepseek_streamed_completion_preserves_length_finish_reason():
    def handler(_request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"delta":{"reasoning_content":"继续规划。"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
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
            deltas = []
            result = await client.complete_streamed(
                system_prompt="规划任务。",
                user_prompt="开始",
                thinking=True,
                on_delta=deltas.append,
            )
            return result, deltas

    result, deltas = asyncio.run(run_completion())

    assert result.content == ""
    assert result.reasoning_content == "继续规划。"
    assert result.finish_reason == "length"
    assert [(delta.kind, delta.delta) for delta in deltas] == [
        ("reasoning", "继续规划。"),
    ]


def test_stream_packet_preserves_both_fields_and_finish():
    def handler(_request):
        return httpx.Response(200, text='data: {"choices":[{"delta":{"reasoning_content":"比较。","content":"结论。"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.deepseek.com") as http_client:
            client = DeepSeekClient(api_key="test_key", model="deepseek-v4-flash", http_client=http_client)
            return [event async for event in client.stream_complete_events(system_prompt="测试", user_prompt="比较", thinking=True)]

    assert [(e.kind, e.delta) for e in asyncio.run(run())] == [
        ("reasoning", "比较。"), ("content", "结论。"), ("finish", "stop")
    ]


def test_direct_completion_records_only_model_parameters_and_usage(monkeypatch):
    observations = []
    updates = []
    generation = object()

    @contextmanager
    def observe_generation(**kwargs):
        observations.append(kwargs)
        yield generation

    monkeypatch.setattr(
        deepseek_module,
        "langfuse_generation",
        observe_generation,
        raising=False,
    )
    monkeypatch.setattr(
        deepseek_module,
        "update_langfuse_generation",
        lambda target, **kwargs: updates.append((target, kwargs)),
        raising=False,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "pong"}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 7,
                    "total_tokens": 19,
                    "ignored_provider_field": 99,
                },
            },
        )

    async def run_completion():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.deepseek.com",
        ) as http_client:
            return await DeepSeekClient(
                api_key="test_key",
                model="deepseek-v4-flash",
                http_client=http_client,
            ).complete(
                system_prompt="secret system prompt",
                user_prompt="secret user prompt",
                temperature=0.2,
                max_tokens=128,
                thinking=False,
            )

    result = asyncio.run(run_completion())

    assert result.content == "pong"
    assert observations == [
        {
            "name": "deepseek.chat.completions",
            "model": "deepseek-v4-flash",
            "model_parameters": {
                "temperature": 0.2,
                "max_tokens": 128,
                "thinking": False,
            },
        }
    ]
    assert updates == [
        (
            generation,
            {"usage_details": {"input": 12, "output": 7, "total": 19}},
        )
    ]
    assert "secret" not in str(observations)
    assert "secret" not in str(updates)


def test_streamed_completion_requests_and_records_usage_without_forwarding_it(
    monkeypatch,
):
    captured = {}
    updates = []
    generation = object()

    @contextmanager
    def observe_generation(**_kwargs):
        yield generation

    monkeypatch.setattr(
        deepseek_module,
        "langfuse_generation",
        observe_generation,
        raising=False,
    )
    monkeypatch.setattr(
        deepseek_module,
        "update_langfuse_generation",
        lambda target, **kwargs: updates.append((target, kwargs)),
        raising=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.read())
        body = (
            'data: {"choices":[{"delta":{"content":"答案"},"finish_reason":"stop"}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    async def run_completion():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.deepseek.com",
        ) as http_client:
            deltas = []
            result = await DeepSeekClient(
                api_key="test_key",
                model="deepseek-v4-flash",
                http_client=http_client,
            ).complete_streamed(
                system_prompt="system",
                user_prompt="user",
                on_delta=deltas.append,
            )
            return result, deltas

    result, deltas = asyncio.run(run_completion())

    assert captured["payload"]["stream_options"] == {"include_usage": True}
    assert result.usage == {
        "prompt_tokens": 8,
        "completion_tokens": 2,
        "total_tokens": 10,
    }
    assert [(delta.kind, delta.delta) for delta in deltas] == [
        ("content", "答案")
    ]
    assert updates == [
        (
            generation,
            {"usage_details": {"input": 8, "output": 2, "total": 10}},
        )
    ]


def test_direct_completion_records_only_stable_provider_error_code(monkeypatch):
    updates = []
    generation = object()

    @contextmanager
    def observe_generation(**_kwargs):
        yield generation

    monkeypatch.setattr(deepseek_module, "langfuse_generation", observe_generation)
    monkeypatch.setattr(
        deepseek_module,
        "update_langfuse_generation",
        lambda target, **kwargs: updates.append((target, kwargs)),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="provider-secret-error")

    async def run_completion():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.deepseek.com",
        ) as http_client:
            return await DeepSeekClient(
                api_key="test_key",
                model="deepseek-v4-flash",
                http_client=http_client,
            ).complete(system_prompt="system", user_prompt="user")

    with pytest.raises(LlmProviderError, match="provider-secret-error"):
        asyncio.run(run_completion())

    assert updates == [
        (
            generation,
            {"level": "ERROR", "status_message": "llm_http_error"},
        )
    ]
    assert "provider-secret-error" not in str(updates)
