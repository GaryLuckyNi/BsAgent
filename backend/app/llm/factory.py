import asyncio
from collections.abc import AsyncIterator, Callable
from threading import Lock
from typing import Any, Protocol

from app.core.config import settings
from app.llm.deepseek import DeepSeekClient, LlmConfigurationError
from app.llm.schemas import LlmCompletionResult, LlmStreamDelta

_REASONING_LANGUAGE_INSTRUCTION = (
    "\n\n当模型生成 reasoning_content 时，必须使用中文表达；专有名词、代码标识符和原始字段名可以保留英文。"
)
_client_lock = Lock()
_cached_client: Any | None = None
_cached_client_fingerprint: tuple[object, ...] | None = None


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
    request_system_prompt = (
        f"{system_prompt}{_REASONING_LANGUAGE_INSTRUCTION}"
        if thinking
        else system_prompt
    )
    result = _invoke_llm_completion_once(
        llm_client,
        system_prompt=request_system_prompt,
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
    sync_native_stream_completion = getattr(
        llm_client,
        "complete_streamed_sync",
        None,
    )
    if on_delta is not None and callable(sync_native_stream_completion):
        return sync_native_stream_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking=thinking,
            on_delta=on_delta,
        )
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
    global _cached_client, _cached_client_fingerprint
    if settings.llm_provider == "deepseek":
        fingerprint = (
            settings.llm_provider,
            settings.deepseek_api_key,
            settings.deepseek_model,
            settings.deepseek_base_url,
            settings.llm_timeout_seconds,
        )
        with _client_lock:
            if (
                _cached_client is not None
                and _cached_client_fingerprint == fingerprint
            ):
                return _cached_client
            _close_cached_client()
            _cached_client = DeepSeekClient(
                api_key=settings.deepseek_api_key,
                model=settings.deepseek_model,
                base_url=settings.deepseek_base_url,
                timeout_seconds=settings.llm_timeout_seconds,
                persistent_sync_http=True,
            )
            _cached_client_fingerprint = fingerprint
            return _cached_client
    raise LlmConfigurationError(f"Unsupported LLM provider: {settings.llm_provider}")


def maybe_build_llm_client() -> LlmClient:
    return build_llm_client()


def close_llm_client() -> None:
    with _client_lock:
        _close_cached_client()


def _close_cached_client() -> None:
    global _cached_client, _cached_client_fingerprint
    if _cached_client is not None:
        close = getattr(_cached_client, "close", None)
        if callable(close):
            close()
    _cached_client = None
    _cached_client_fingerprint = None
