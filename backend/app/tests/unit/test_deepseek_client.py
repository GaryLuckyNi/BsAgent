import asyncio
import json
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from app.llm import deepseek as deepseek_module
from app.llm.deepseek import DeepSeekClient, LlmConfigurationError, LlmProviderError
from app.operations.langfuse_observability import configure_langfuse
from app.security.prompt_injection import system_boundary


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
                "model": "deepseek-v4-flash-actual",
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
    assert result.model == "deepseek-v4-flash-actual"
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


@pytest.mark.parametrize(
    ("mode", "capture_reasoning", "expected_reasoning"),
    [
        ("local_content", False, False),
        ("local_debug", True, True),
    ],
)
def test_deepseek_langfuse_output_obeys_content_policy(
    mode, capture_reasoning, expected_reasoning
):
    observations = []

    class Provider:
        def force_flush(self, **_kwargs):
            pass

        def shutdown(self):
            pass

    class Client:
        def start_as_current_observation(self, **kwargs):
            observation = {**kwargs, "updates": []}
            observations.append(observation)

            @contextmanager
            def manager():
                yield SimpleNamespace(
                    update=lambda **attributes: observation["updates"].append(
                        attributes
                    )
                )

            return manager()

        def shutdown(self):
            pass

    runtime = configure_langfuse(
        config=SimpleNamespace(
            langfuse_enabled=True,
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            langfuse_base_url="https://langfuse.test",
            langfuse_sample_rate=1.0,
            langfuse_content_mode=mode,
            langfuse_capture_reasoning=capture_reasoning,
            langfuse_max_content_chars=1000,
            service_name="test",
            app_env="test",
        ),
        client_factory=lambda **_kwargs: Client(),
        provider_factory=lambda **_kwargs: Provider(),
        handler_factory=lambda **_kwargs: object(),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "answer",
                            "reasoning_content": "private reasoning",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
        )

    async def complete():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.deepseek.com",
        ) as http_client:
            return await DeepSeekClient(
                api_key="test_key",
                model="deepseek-v4-flash",
                http_client=http_client,
            ).complete(system_prompt="system", user_prompt="user")

    try:
        asyncio.run(complete())
        output = observations[0]["updates"][-1]["output"]
        assert output["content"] == "answer"
        assert ("reasoning_content" in output) is expected_reasoning
        assert observations[0]["metadata"]["content_policy_applied"] is True
    finally:
        if runtime is not None:
            runtime.shutdown()


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


def test_tool_binding_uses_deterministic_temperature_and_disables_thinking():
    client = DeepSeekClient(api_key="test_key", model="deepseek-v4-flash")
    bound = client.bind_tools(
        [{"type": "function", "function": {
            "name": "lookup", "description": "查询", "parameters": {"type": "object", "properties": {}},
        }}],
        tool_choice="auto",
    )

    assert bound.kwargs["temperature"] == 0
    assert bound.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert bound.kwargs["tool_choice"] == "auto"


def test_deepseek_callback_completion_enables_thinking_for_graph_stream(monkeypatch):
    captured = {}

    class FakeBoundModel:
        def invoke(self, messages, config=None):
            from langchain_core.messages import AIMessage
            captured["messages"] = messages
            return AIMessage(content="最终内容", usage_metadata={"input_tokens": 0, "output_tokens": 4, "total_tokens": 4})

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

    assert chunks == ["我先分析"]
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
        ("reasoning", "先核对公司主体。再分析证据。"),
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
        ("content", "## 结论\n\n证据支持该判断。"),
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
    assert deltas == []  # 长度截断结果保留 finish_reason，正文留给调用方重试决策。


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


def test_deepseek_sync_stream_forwards_first_reasoning_chunk_without_async_bridge():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"reasoning_content":"先核对。"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"结论。"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.deepseek.com",
    ) as http_client:
        client = DeepSeekClient(
            api_key="test_key",
            model="deepseek-v4-flash",
            sync_http_client=http_client,
        )
        deltas = []

        result = client.complete_streamed_sync(
            system_prompt="系统",
            user_prompt="用户",
            thinking=True,
            on_delta=deltas.append,
        )

    assert [(item.kind, item.delta) for item in deltas] == [
        ("reasoning", "先核对。"),
        ("content", "结论。"),
    ]
    assert result.reasoning_content == "先核对。"
    assert result.content == "结论。"


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
            "metadata": {
                "request_mode": "non_streaming",
                "system_prompt_chars": len(system_boundary("secret system prompt")),
                "user_prompt_chars": 18,
                "input_chars": len(system_boundary("secret system prompt"))+18,
                "timeout_seconds": 30.0,
            },
            "system_prompt": system_boundary("secret system prompt"),
            "user_prompt": "secret user prompt",
        }
    ]
    assert updates == [
        (
            generation,
            {
                "metadata": {
                    "request_mode": "non_streaming",
                    "system_prompt_chars": len(system_boundary("secret system prompt")),
                    "user_prompt_chars": 18,
                    "input_chars": len(system_boundary("secret system prompt"))+18,
                    "timeout_seconds": 30.0,
                    "response_chars": 4,
                    "finish_reason": None,
                },
                "model": "deepseek-v4-flash",
                "output": {
                    "content": "pong",
                    "reasoning_content": "",
                    "finish_reason": None,
                },
                "usage_details": {"input": 12, "output": 7, "total": 19},
            },
        )
    ]
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
            'data: {"model":"deepseek-v4-flash-actual","choices":[{"delta":{"content":"答案"},"finish_reason":"stop"}]}\n\n'
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
    assert result.model == "deepseek-v4-flash-actual"
    assert [(delta.kind, delta.delta) for delta in deltas] == [
        ("content", "答案")
    ]
    assert len(updates) == 2
    assert updates[0][0] is generation
    assert isinstance(updates[0][1]["completion_start_time"], datetime)
    assert updates[0][1]["metadata"]["first_token_received"] is True
    assert updates[0][1]["metadata"]["first_token_kind"] == "content"
    assert updates[1][0] is generation
    assert updates[1][1] == {
            "metadata": {
                "request_mode": "streaming",
                "system_prompt_chars": len(system_boundary("system")),
                "user_prompt_chars": 4,
                "input_chars": len(system_boundary("system"))+4,
                "timeout_seconds": 30.0,
                "first_token_received": True,
                "content_chars": 2,
                "reasoning_chars": 0,
                "finish_reason": "stop",
                "ttft_ms": updates[1][1]["metadata"]["ttft_ms"],
            },
            "model": "deepseek-v4-flash-actual",
            "output": {
                "content": "答案",
                "reasoning_content": "",
                "finish_reason": "stop",
            },
            "usage_details": {"input": 8, "output": 2, "total": 10},
        }
    assert updates[1][1]["metadata"]["ttft_ms"] >= 0


def test_streamed_timeout_records_phase_and_whether_first_token_arrived(monkeypatch):
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

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider read stalled", request=request)

    async def run_completion():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.deepseek.com",
        ) as http_client:
            return await DeepSeekClient(
                api_key="test_key",
                model="deepseek-v4-flash",
                http_client=http_client,
            ).complete_streamed(
                system_prompt="system",
                user_prompt="user",
                on_delta=lambda _delta: None,
            )

    with pytest.raises(LlmProviderError) as exc_info:
        asyncio.run(run_completion())

    assert exc_info.value.code == "llm_timeout"
    assert exc_info.value.timeout_phase == "read"
    assert updates == [
        (
            generation,
            {
                "level": "ERROR",
                "status_message": "llm_timeout:read:before_first_token",
                "metadata": {
                    "request_mode": "streaming",
                    "system_prompt_chars": len(system_boundary("system")),
                    "user_prompt_chars": 4,
                    "input_chars": len(system_boundary("system"))+4,
                    "timeout_seconds": 30.0,
                    "first_token_received": False,
                    "content_chars": 0,
                    "reasoning_chars": 0,
                    "timeout_phase": "read",
                },
            },
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

    with pytest.raises(LlmProviderError, match="模型服务请求失败。") as caught:
        asyncio.run(run_completion())
    assert caught.value.__context__ is None
    assert "provider-secret-error" not in str(caught.value)

    assert updates == [
        (
            generation,
            {"level": "ERROR", "status_message": "llm_http_error"},
        )
    ]
    assert "provider-secret-error" not in str(updates)


def test_sync_stream_records_usage_ttft_and_actual_model(monkeypatch):
    updates = []
    captured = {}
    generation = object()

    @contextmanager
    def observe_generation(**_kwargs):
        yield generation

    monkeypatch.setattr(deepseek_module, "langfuse_generation", observe_generation)
    monkeypatch.setattr(
        deepseek_module, "update_langfuse_generation",
        lambda target, **kwargs: updates.append((target, kwargs)),
    )

    def handler(request):
        captured["payload"] = json.loads(request.read())
        return httpx.Response(200, text=(
            'data: {"model":"actual-model","choices":[{"delta":{"reasoning_content":"私有推理"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"回答"},"finish_reason":"stop"}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10}}\n\n'
            'data: [DONE]\n\n'
        ))

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.deepseek.com") as http_client:
        client = DeepSeekClient(api_key="test_key", model="configured-model", sync_http_client=http_client)
        deltas = []
        result = client.complete_streamed_sync(system_prompt="system", user_prompt="user", on_delta=deltas.append)

    assert captured["payload"]["stream_options"] == {"include_usage": True}
    assert result.model == "actual-model"
    assert result.usage == {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}
    assert [(delta.kind, delta.delta) for delta in deltas] == [("reasoning", "私有推理"), ("content", "回答")]
    assert updates[0][1]["metadata"]["first_token_kind"] == "reasoning"
    assert isinstance(updates[0][1]["completion_start_time"], datetime)
    assert updates[-1][1]["usage_details"] == {"input": 8, "output": 2, "total": 10}
    assert updates[-1][1]["metadata"]["ttft_ms"] >= 0


def test_sync_stream_timeout_preserves_phase_after_first_token(monkeypatch):
    updates = []

    @contextmanager
    def observe_generation(**_kwargs):
        yield object()

    monkeypatch.setattr(deepseek_module, "langfuse_generation", observe_generation)
    monkeypatch.setattr(
        deepseek_module, "update_langfuse_generation",
        lambda _target, **kwargs: updates.append(kwargs),
    )

    class StalledStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            raise httpx.ReadTimeout("private timeout details")

    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=StalledStream())), base_url="https://api.deepseek.com") as http_client:
        client = DeepSeekClient(api_key="test_key", model="model", sync_http_client=http_client)
        with pytest.raises(LlmProviderError) as error:
            client.complete_streamed_sync(system_prompt="system", user_prompt="user", on_delta=lambda _delta: None)

    assert error.value.timeout_phase == "read"
    assert updates[-1]["status_message"] == "llm_timeout:read:after_first_token"
    assert updates[-1]["metadata"]["content_chars"] == 1
    assert "private timeout details" not in str(updates)


def test_async_stream_model_does_not_leak_between_completions():
    responses = iter([
        'data: {"model":"first-model","choices":[{"delta":{"content":"a"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"b"}}]}\n\n',
    ])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=next(responses))), base_url="https://api.deepseek.com") as http_client:
            client = DeepSeekClient(api_key="test_key", model="configured-model", http_client=http_client)
            first = await client.complete_streamed(system_prompt="system", user_prompt="user", on_delta=lambda _delta: None)
            second = await client.complete_streamed(system_prompt="system", user_prompt="user", on_delta=lambda _delta: None)
            return first, second

    first, second = asyncio.run(run())
    assert first.model == "first-model"
    assert second.model == "configured-model"


def test_stream_callback_cancellation_propagates_and_does_not_close_shared_client():
    def cancel(_delta):
        raise asyncio.CancelledError()

    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, text='data: {"choices":[{"delta":{"content":"a"}}]}\n\n')), base_url="https://api.deepseek.com") as http_client:
        client = DeepSeekClient(api_key="test_key", model="model", sync_http_client=http_client)
        with pytest.raises(asyncio.CancelledError):
            client.complete_streamed_sync(system_prompt="system", user_prompt="user", on_delta=cancel)
        client.close()
        assert not http_client.is_closed
