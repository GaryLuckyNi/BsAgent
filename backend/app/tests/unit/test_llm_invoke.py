import asyncio

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
