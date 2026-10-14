import json
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.llm.schemas import LlmCompletionResult, LlmStreamDelta
from app.operations.langfuse_observability import (
    langfuse_generation,
    update_langfuse_generation,
)


class LlmConfigurationError(RuntimeError):
    pass


class LlmProviderError(RuntimeError):
    def __init__(self, *, code: str, message: str, status_code: int | None = None) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DeepSeekClient:
    provider = "deepseek"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
        sync_http_client: httpx.Client | None = None,
        persistent_sync_http: bool = False,
    ) -> None:
        if not api_key:
            raise LlmConfigurationError("DeepSeek api key is required.")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client
        self._owns_sync_http_client = sync_http_client is None and persistent_sync_http
        self._sync_http_client = sync_http_client
        if self._sync_http_client is None and persistent_sync_http:
            self._sync_http_client = httpx.Client(
                base_url=base_url,
                timeout=timeout_seconds,
            )
        self._chat_model = ChatOpenAI(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
            http_async_client=http_client,
            extra_body={"thinking": {"type": "disabled"}},
        )

    def bind_tools(self, tools: list[dict[str, Any]], *, tool_choice: str):
        return self._chat_model.bind(extra_body={"thinking": {"type": "disabled"}}).bind_tools(
            tools, tool_choice=tool_choice
        )

    def complete_with_streaming_callbacks(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool = True,
    ) -> LlmCompletionResult:
        bind_options: dict[str, Any] = {
            "max_tokens": max_tokens,
            "extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"}},
        }
        if thinking:
            bind_options["reasoning_effort"] = "high"
        else:
            bind_options["temperature"] = temperature
        response = self._chat_model.bind(**bind_options).invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ]
        )
        content = response.content if isinstance(response.content, str) else ""
        additional_kwargs = getattr(response, "additional_kwargs", None) or {}
        response_metadata = getattr(response, "response_metadata", None) or {}
        reasoning_content = additional_kwargs.get("reasoning_content")
        if not isinstance(reasoning_content, str):
            reasoning_content = response_metadata.get("reasoning_content", "")
        if not isinstance(reasoning_content, str):
            reasoning_content = ""
        return LlmCompletionResult(
            content=content,
            reasoning_content=reasoning_content,
            model=self._model,
            provider=self.provider,
            usage=response.usage_metadata or {},
            finish_reason=response_metadata.get("finish_reason"),
        )

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool | None = None,
    ) -> LlmCompletionResult:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if thinking is not None:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}

        with langfuse_generation(
            name="deepseek.chat.completions",
            model=self._model,
            model_parameters={
                "temperature": temperature,
                "max_tokens": max_tokens,
                "thinking": bool(thinking),
            },
            input_data=payload["messages"],
        ) as generation:
            started_at = time.monotonic()
            try:
                if self._http_client is not None:
                    result = await self._complete_with_client(self._http_client, payload)
                else:
                    async with httpx.AsyncClient(
                        base_url=self._base_url,
                        timeout=self._timeout_seconds,
                    ) as client:
                        result = await self._complete_with_client(client, payload)
            except LlmProviderError as exc:
                update_langfuse_generation(
                    generation,
                    level="ERROR",
                    status_message=exc.code,
                )
                raise
            _record_generation(
                generation,
                usage=result.usage,
                finish_reason=result.finish_reason,
                ttft_ms=int((time.monotonic() - started_at) * 1000),
                output=result.content,
            )
            return result

    async def _complete_with_client(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
    ) -> LlmCompletionResult:
        try:
            response = await client.post(
                "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LlmProviderError(
                code="llm_http_error",
                message=exc.response.text,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.TimeoutException as exc:
            raise LlmProviderError(code="llm_timeout", message=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise LlmProviderError(code="llm_unreachable", message=str(exc)) from exc

        data = response.json()
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmProviderError(
                code="llm_invalid_response",
                message="DeepSeek response did not contain choices[0].message.content.",
            ) from exc

        return LlmCompletionResult(
            content=content,
            reasoning_content=(
                choice["message"].get("reasoning_content", "")
                if isinstance(choice.get("message"), dict)
                and isinstance(choice["message"].get("reasoning_content", ""), str)
                else ""
            ),
            model=self._model,
            provider=self.provider,
            usage=data.get("usage", {}),
            finish_reason=choice.get("finish_reason"),
        )

    async def stream_complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool | None = None,
    ) -> AsyncIterator[str]:
        async for event in self.stream_complete_events(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking=thinking,
        ):
            if event.kind == "content":
                yield event.delta

    async def stream_complete_events(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool | None = None,
    ) -> AsyncIterator[LlmStreamDelta]:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if thinking is not None:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}

        if self._http_client is not None:
            async for event in self._stream_events_with_client(
                self._http_client,
                payload,
            ):
                yield event
            return

        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
        ) as client:
            async for event in self._stream_events_with_client(client, payload):
                yield event

    async def complete_streamed(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool = True,
        on_delta: Callable[[LlmStreamDelta], None],
    ) -> LlmCompletionResult:
        with langfuse_generation(
            name="deepseek.chat.completions",
            model=self._model,
            model_parameters={
                "temperature": temperature,
                "max_tokens": max_tokens,
                "thinking": thinking,
            },
            input_data=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        ) as generation:
            result = await self._collect_async_stream(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking=thinking,
                on_delta=on_delta,
                generation=generation,
            )
            return result

    async def _collect_async_stream(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        thinking: bool,
        on_delta: Callable[[LlmStreamDelta], None],
        generation: Any | None,
    ) -> LlmCompletionResult:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, int] = {}
        started_at = time.monotonic()
        first_token_at: float | None = None
        try:
            async for event in self.stream_complete_events(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking=thinking,
            ):
                if event.kind == "finish":
                    finish_reason = event.delta or None
                    continue
                if event.kind == "usage":
                    usage = event.usage
                    continue
                first_token_at = first_token_at or time.monotonic()
                on_delta(event)
                if event.kind == "content":
                    content_parts.append(event.delta)
                elif event.kind == "reasoning":
                    reasoning_parts.append(event.delta)
        except LlmProviderError as exc:
            update_langfuse_generation(generation, level="ERROR", status_message=exc.code)
            raise
        result = LlmCompletionResult(
            content="".join(content_parts),
            reasoning_content="".join(reasoning_parts),
            model=self._model,
            provider=self.provider,
            usage=usage,
            finish_reason=finish_reason,
        )
        _record_generation(
            generation,
            usage=usage,
            finish_reason=finish_reason,
            ttft_ms=(int((first_token_at - started_at) * 1000) if first_token_at else None),
            output=result.content,
        )
        return result

    def complete_streamed_sync(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool = True,
        on_delta: Callable[[LlmStreamDelta], None],
    ) -> LlmCompletionResult:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        with langfuse_generation(
            name="deepseek.chat.completions",
            model=self._model,
            model_parameters={
                "temperature": temperature,
                "max_tokens": max_tokens,
                "thinking": thinking,
            },
            input_data=payload["messages"],
        ) as generation:
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            finish_reason: str | None = None
            usage: dict[str, int] = {}
            started_at = time.monotonic()
            first_token_at: float | None = None
            try:
                for event in self._stream_events_sync(payload):
                    if event.kind == "finish":
                        finish_reason = event.delta or None
                        continue
                    if event.kind == "usage":
                        usage = event.usage
                        continue
                    first_token_at = first_token_at or time.monotonic()
                    on_delta(event)
                    if event.kind == "content":
                        content_parts.append(event.delta)
                    elif event.kind == "reasoning":
                        reasoning_parts.append(event.delta)
            except LlmProviderError as exc:
                update_langfuse_generation(
                    generation,
                    level="ERROR",
                    status_message=exc.code,
                )
                raise

            result = LlmCompletionResult(
                content="".join(content_parts),
                reasoning_content="".join(reasoning_parts),
                model=self._model,
                provider=self.provider,
                usage=usage,
                finish_reason=finish_reason,
            )
            _record_generation(
                generation,
                usage=usage,
                finish_reason=finish_reason,
                ttft_ms=(int((first_token_at - started_at) * 1000) if first_token_at else None),
                output=result.content,
            )
            return result

    def _stream_events_sync(
        self,
        payload: dict[str, Any],
    ) -> Iterator[LlmStreamDelta]:
        if self._sync_http_client is not None:
            yield from self._stream_events_with_sync_client(
                self._sync_http_client,
                payload,
            )
            return
        with httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
        ) as client:
            yield from self._stream_events_with_sync_client(client, payload)

    def _stream_events_with_sync_client(
        self,
        client: httpx.Client,
        payload: dict[str, Any],
    ) -> Iterator[LlmStreamDelta]:
        try:
            with client.stream(
                "POST",
                "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    yield from _parse_openai_stream_events(line)
        except httpx.HTTPStatusError as exc:
            raise LlmProviderError(
                code="llm_http_error",
                message=exc.response.text,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.TimeoutException as exc:
            raise LlmProviderError(code="llm_timeout", message=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise LlmProviderError(code="llm_unreachable", message=str(exc)) from exc

    def close(self) -> None:
        if self._owns_sync_http_client and self._sync_http_client is not None:
            self._sync_http_client.close()
        self._sync_http_client = None

    async def _stream_events_with_client(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
    ) -> AsyncIterator[LlmStreamDelta]:
        try:
            async with client.stream(
                "POST",
                "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    for event in _parse_openai_stream_events(line):
                        yield event
        except httpx.HTTPStatusError as exc:
            raise LlmProviderError(
                code="llm_http_error",
                message=exc.response.text,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.TimeoutException as exc:
            raise LlmProviderError(code="llm_timeout", message=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise LlmProviderError(code="llm_unreachable", message=str(exc)) from exc


def _parse_openai_stream_events(line: str) -> Iterator[LlmStreamDelta]:
    if not line.startswith("data:"):
        return
    data = line.removeprefix("data:").strip()
    if not data or data == "[DONE]":
        return
    try:
        payload = json.loads(data)
    except (TypeError, json.JSONDecodeError):
        return
    usage = payload.get("usage")
    if isinstance(usage, dict):
        normalized_usage = {
            key: value
            for key, value in usage.items()
            if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
        }
        if normalized_usage:
            yield LlmStreamDelta(kind="usage", usage=normalized_usage)
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return
    choice = choices[0]
    if not isinstance(choice, dict):
        return
    delta = choice.get("delta", {})
    if not isinstance(delta, dict):
        return
    reasoning = delta.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        yield LlmStreamDelta(kind="reasoning", delta=reasoning)
    content = delta.get("content")
    if isinstance(content, str) and content:
        yield LlmStreamDelta(kind="content", delta=content)
    finish_reason = choice.get("finish_reason")
    if isinstance(finish_reason, str) and finish_reason:
        yield LlmStreamDelta(kind="finish", delta=finish_reason)
    return


def _record_generation(
    generation: Any | None,
    *,
    usage: dict[str, Any],
    finish_reason: str | None,
    ttft_ms: int | None,
    output: str,
) -> None:
    mapping = {
        "prompt_tokens": "input",
        "completion_tokens": "output",
        "total_tokens": "total",
        "input_tokens": "input",
        "output_tokens": "output",
    }
    usage_details = {
        target: value
        for source, target in mapping.items()
        if isinstance((value := usage.get(source)), int) and not isinstance(value, bool)
    }
    attributes: dict[str, Any] = {
        "output": output,
        "metadata": {
            "finish_reason": finish_reason,
            "ttft_ms": ttft_ms,
        }
    }
    if usage_details:
        attributes["usage_details"] = usage_details
    update_langfuse_generation(generation, **attributes)
