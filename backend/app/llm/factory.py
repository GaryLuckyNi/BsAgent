import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from app.core.config import settings
from app.llm.deepseek import DeepSeekClient, LlmConfigurationError
from app.llm.schemas import LlmCompletionResult, LlmStreamDelta


class LlmClient(Protocol):
    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool | None = None,
    ) -> LlmCompletionResult: ...

    async def stream_complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool | None = None,
    ) -> AsyncIterator[str]: ...


class ToolCallingLlmClient(LlmClient, Protocol):
    def bind_tools(self, tools: list[dict[str, Any]], *, tool_choice: str): ...


def invoke_llm_completion(
    llm_client: LlmClient,
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.0,
    max_tokens: int = 256,
    thinking: bool = True,
    on_delta: Callable[[LlmStreamDelta], None] | None = None,
    retry_without_thinking_on_empty: bool = False,
) -> LlmCompletionResult:
    result = _invoke_llm_completion_once(
        llm_client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        thinking=thinking,
        on_delta=on_delta,
    )
    if retry_without_thinking_on_empty and thinking and not result.content.strip():
        return _invoke_llm_completion_once(
            llm_client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking=False,
            on_delta=on_delta,
        )
    return result


def _invoke_llm_completion_once(
    llm_client: LlmClient,
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    thinking: bool,
    on_delta: Callable[[LlmStreamDelta], None] | None,
) -> LlmCompletionResult:
    native_stream_completion = getattr(llm_client, "complete_streamed", None)
    if on_delta is not None and callable(native_stream_completion):
        return asyncio.run(
            native_stream_completion(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking=thinking,
                on_delta=on_delta,
            )
        )
    callback_completion = getattr(
        llm_client,
        "complete_with_streaming_callbacks",
        None,
    )
    kwargs = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "thinking": thinking,
    }
    if callable(callback_completion):
        return callback_completion(**kwargs)
    return asyncio.run(llm_client.complete(**kwargs))


def build_llm_client() -> LlmClient:
    if settings.llm_provider == "deepseek":
        return DeepSeekClient(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    raise LlmConfigurationError(f"Unsupported LLM provider: {settings.llm_provider}")


def maybe_build_llm_client() -> LlmClient:
    return build_llm_client()
