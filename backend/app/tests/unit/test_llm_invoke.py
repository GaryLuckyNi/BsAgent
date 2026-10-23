import asyncio
import json

from app.llm import factory as llm_factory
from app.llm.factory import invoke_llm_completion
from app.llm.schemas import LlmCompletionResult


def test_invoke_llm_completion_prefers_graph_streaming_callback_path():
    class Client:
        def __init__(self):
            self.kwargs = None

        def complete_with_streaming_callbacks(self, **kwargs):
            self.kwargs = kwargs
            return LlmCompletionResult(
                content="ok",
                model="test",
                provider="test",
            )

        async def complete(self, **_kwargs):
            raise AssertionError("should not use async fallback")

    client = Client()
    result = invoke_llm_completion(
        client,
        system_prompt="system",
        user_prompt="user",
        max_tokens=512,
        thinking=True,
    )

    assert result.content == "ok"
    assert client.kwargs["thinking"] is True


def test_invoke_llm_completion_falls_back_to_async_provider_contract():
    class Client:
        def __init__(self):
            self.kwargs = None

        async def complete(self, **kwargs):
            await asyncio.sleep(0)
            self.kwargs = kwargs
            return LlmCompletionResult(
                content="fallback",
                model="test",
                provider="test",
            )

    client = Client()
    result = invoke_llm_completion(
        client,
        system_prompt="system",
        user_prompt="user",
        thinking=True,
    )

    assert result.content == "fallback"
    assert client.kwargs["thinking"] is True


def test_invoke_llm_completion_prefers_native_stream_when_handler_is_present():
    class Client:
        def __init__(self):
            self.kwargs = None

        async def complete_streamed(self, **kwargs):
            self.kwargs = kwargs
            kwargs["on_delta"]("native-delta")
            return LlmCompletionResult(
                content="native",
                model="test",
                provider="test",
            )

        def complete_with_streaming_callbacks(self, **_kwargs):
            raise AssertionError("native provider stream should win")

    received = []
    client = Client()
    result = invoke_llm_completion(
        client,
        system_prompt="system",
        user_prompt="user",
        thinking=True,
        on_delta=received.append,
    )

    assert result.content == "native"
    assert received == ["native-delta"]
    assert client.kwargs["thinking"] is True


def test_invoke_llm_completion_prefers_sync_native_stream_without_event_loop_bridge():
    class Client:
        def __init__(self):
            self.kwargs = None

        def complete_streamed_sync(self, **kwargs):
            self.kwargs = kwargs
            kwargs["on_delta"]("sync-delta")
            return LlmCompletionResult(content="sync", model="test", provider="test")

        async def complete(self, **_kwargs):
            raise AssertionError("同步原生流存在时不应进入 asyncio fallback")

    client = Client()
    received = []

    result = invoke_llm_completion(
        client,
        system_prompt="system",
        user_prompt="user",
        thinking=True,
        on_delta=received.append,
    )

    assert result.content == "sync"
    assert received == ["sync-delta"]
    assert client.kwargs["thinking"] is True


def test_thinking_request_asks_model_to_keep_raw_reasoning_in_chinese():
    class Client:
        def __init__(self):
            self.system_prompt = ""

        async def complete(self, **kwargs):
            self.system_prompt = kwargs["system_prompt"]
            self.user_prompt = kwargs["user_prompt"]
            return LlmCompletionResult(content="完成", model="test", provider="test")

    client = Client()

    invoke_llm_completion(
        client,
        system_prompt="你是研究助手。",
        user_prompt=json.dumps({"question": "分析问题"}, ensure_ascii=False),
        thinking=True,
    )

    assert "reasoning_content" in client.system_prompt
    assert client.system_prompt.startswith("【推理语言硬性要求】")
    assert client.system_prompt.count("【推理语言硬性要求】") == 1
    assert json.loads(client.user_prompt) == {"question": "分析问题"}


def test_invoke_llm_completion_retries_partial_length_output_without_streaming_duplicate():
    class Client:
        def __init__(self):
            self.stream_calls = 0
            self.complete_calls = []

        def complete_streamed_sync(self, **kwargs):
            self.stream_calls += 1
            kwargs["on_delta"]("partial-delta")
            return LlmCompletionResult(
                content="半截回答",
                model="test",
                provider="test",
                finish_reason="length",
            )

        async def complete(self, **kwargs):
            self.complete_calls.append(kwargs)
            return LlmCompletionResult(
                content="完整回答",
                model="test",
                provider="test",
                finish_reason="stop",
            )

    client = Client()
    received = []

    result = invoke_llm_completion(
        client,
        system_prompt="system",
        user_prompt="user",
        thinking=True,
        on_delta=received.append,
        retry_without_thinking_on_length=True,
    )

    assert result.content == "完整回答"
    assert received == ["partial-delta"]
    assert client.stream_calls == 1
    assert [call["thinking"] for call in client.complete_calls] == [False]


def test_llm_factory_reuses_client_until_runtime_is_closed(monkeypatch):
    created = []

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            created.append(self)

        def close(self):
            self.closed = True

    llm_factory.close_llm_client()
    monkeypatch.setattr(llm_factory, "DeepSeekClient", Client)

    first = llm_factory.build_llm_client()
    second = llm_factory.build_llm_client()
    llm_factory.close_llm_client()
    third = llm_factory.build_llm_client()
    llm_factory.close_llm_client()

    assert first is second
    assert first.closed is True
    assert third is not first
    assert len(created) == 2


def test_invoke_llm_completion_retries_empty_thinking_output_once_without_thinking():
    class Client:
        def __init__(self):
            self.calls = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            content = "" if len(self.calls) == 1 else "补偿后的正文"
            return LlmCompletionResult(
                content=content,
                model="test",
                provider="test",
                finish_reason="length" if not content else "stop",
            )

    client = Client()
    result = invoke_llm_completion(
        client,
        system_prompt="system",
        user_prompt="user",
        thinking=True,
        retry_without_thinking_on_empty=True,
    )

    assert result.content == "补偿后的正文"
    assert [call["thinking"] for call in client.calls] == [True, False]
