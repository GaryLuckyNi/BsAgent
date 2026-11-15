import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.llm.errors import (
    PROVIDER_TRANSPORT_ERRORS,
    LlmConfigurationError,
    LlmProviderError,
    normalized_provider_error,
)
from app.llm.input_guard import ProtectedModel
from app.llm.schemas import LlmCompletionResult, LlmStreamDelta
from app.operations.langfuse_observability import (
    langfuse_generation,
    update_langfuse_generation,
)
from app.security.http_response import async_bounded_request, async_bounded_stream, bounded_stream
from app.security.model_input import check_tool_definitions, protect_model_prompts
from app.security.model_output import (
    IncrementalOutput,
    ModelOutputRejected,
    closing_async_model_source,
    closing_model_source,
    output_view,
    protect_completion,
)
from app.security.sdk_http import BoundedAsyncClient, BoundedClient, RequestScopedAsyncClient

_stream_response_model: ContextVar[str | None] = ContextVar(
    "deepseek_stream_response_model", default=None
)


_stream_received = ContextVar("deepseek_received", default=None)


def _mark_received(event):
    state = _stream_received.get()
    if state is None:
        state = {"first": None, "content_chars": 0, "reasoning_chars": 0}
        _stream_received.set(state)
    if event.kind in {"content", "reasoning"}:
        state[event.kind + "_chars"] += len(event.delta)
        if state["first"] is None:
            state["first"] = (event.kind, perf_counter(), datetime.now(UTC))


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
        self._sdk_sync_client = BoundedClient(timeout_seconds=timeout_seconds, delegate=self._sync_http_client, streaming_is_sse=True, budget_kind="model")
        self._sdk_async_client = RequestScopedAsyncClient(timeout_seconds=timeout_seconds,budget_kind="model") if http_client is None else BoundedAsyncClient(timeout_seconds=timeout_seconds, delegate=http_client, streaming_is_sse=True,budget_kind="model")
        self._chat_model = ChatOpenAI(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=self._sdk_sync_client,
            http_async_client=self._sdk_async_client,
            extra_body={"thinking": {"type": "disabled"}},
        )

    def bind_tools(self, tools: list[dict[str, Any]], *, tool_choice: str):
        check_tool_definitions(tools)
        return ProtectedModel(self._chat_model.bind_tools(
            tools,
            tool_choice=tool_choice,
            temperature=0,
            extra_body={"thinking": {"type": "disabled"}},
        ))

    def complete_with_streaming_callbacks(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool = True,
    ) -> LlmCompletionResult:
        system_prompt, user_prompt = protect_model_prompts(system_prompt, user_prompt)
        bind_options: dict[str, Any] = {
            "max_tokens": max_tokens,
            "extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"}},
        }
        if thinking:
            bind_options["reasoning_effort"] = "high"
        else:
            bind_options["temperature"] = temperature
        failure = None
        try:
            response = ProtectedModel(self._chat_model.bind(**bind_options)).invoke([
                SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
        except PROVIDER_TRANSPORT_ERRORS as error:
            failure = normalized_provider_error(error)
        if failure is not None:
            raise failure
        content = response.content if isinstance(response.content, str) else ""
        additional_kwargs = getattr(response, "additional_kwargs", None) or {}
        response_metadata = getattr(response, "response_metadata", None) or {}
        reasoning_content = additional_kwargs.get("reasoning_content")
        if not isinstance(reasoning_content, str):
            reasoning_content = response_metadata.get("reasoning_content", "")
        if not isinstance(reasoning_content, str):
            reasoning_content = ""
        return protect_completion(LlmCompletionResult(
            content=content,
            reasoning_content=reasoning_content,
            model=self._model,
            provider=self.provider,
            usage=response.usage_metadata or {},
            finish_reason=response_metadata.get("finish_reason"),
        ))

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool | None = None,
    ) -> LlmCompletionResult:
        system_prompt, user_prompt = protect_model_prompts(system_prompt, user_prompt)
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

        generation_metadata = _generation_request_metadata(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            request_mode="non_streaming",
            timeout_seconds=self._timeout_seconds,
        )
        with langfuse_generation(
            name="deepseek.chat.completions",
            model=self._model,
            model_parameters={
                "temperature": temperature,
                "max_tokens": max_tokens,
                "thinking": bool(thinking),
            },
            metadata=generation_metadata,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        ) as generation:
            try:
                if self._http_client is not None:
                    result = await self._complete_with_client(
                        self._http_client,
                        payload,
                    )
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
                    status_message=_error_status_message(exc),
                )
                raise
            _record_langfuse_result(
                generation,
                result=result,
                metadata={
                    **generation_metadata,
                    "response_chars": len(result.content),
                    "finish_reason": result.finish_reason,
                },
            )
            return result

    async def _complete_with_client(self, client, payload):
        failure = None
        try:
            response = await async_bounded_request(client, "POST", "/chat/completions", json=payload,
                budget_kind="model",
                timeout_seconds=self._timeout_seconds,
                headers={"Authorization": f"Bearer {self._api_key}"})
            response.raise_for_status()
        except PROVIDER_TRANSPORT_ERRORS as error:
            failure = normalized_provider_error(error)
        if failure is not None:
            raise failure
        invalid = False
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            if not isinstance(message.get("content"), str):
                raise TypeError
            result = LlmCompletionResult(content=message["content"],
                reasoning_content=message.get("reasoning_content") if isinstance(message.get("reasoning_content"), str) else "",
                model=data.get("model") if isinstance(data.get("model"), str) else self._model,
                provider=self.provider, usage=data.get("usage", {}), finish_reason=choice.get("finish_reason"))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError):
            invalid = True
        if invalid:
            raise LlmProviderError(code="llm_invalid_response")
        return protect_completion(result)

    async def stream_complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool | None = None,
    ) -> AsyncIterator[str]:
        async with closing_async_model_source(self.stream_complete_events(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking=thinking,
        )) as source:
            async for event in source:
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
        system_prompt, user_prompt = protect_model_prompts(system_prompt, user_prompt)
        _stream_response_model.set(None)
        _stream_received.set({"first": None, "content_chars": 0, "reasoning_chars": 0})
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
            async with closing_async_model_source(self._stream_events_with_client(
                self._http_client,
                payload,
            )) as source:
                async for event in source:
                    yield event
            return

        async with (
            httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout_seconds) as client,
            closing_async_model_source(self._stream_events_with_client(client, payload)) as source,
        ):
            async for event in source:
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
        system_prompt, user_prompt = protect_model_prompts(system_prompt, user_prompt)
        generation_metadata = _generation_request_metadata(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            request_mode="streaming",
            timeout_seconds=self._timeout_seconds,
        )
        with langfuse_generation(
            name="deepseek.chat.completions",
            model=self._model,
            model_parameters={
                "temperature": temperature,
                "max_tokens": max_tokens,
                "thinking": thinking,
            },
            metadata=generation_metadata,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        ) as generation:
            stream_started = perf_counter()
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            finish_reason: str | None = None
            usage: dict[str, int] = {}
            first_token_received = False
            ttft_ms: float | None = None
            callback_buffer = IncrementalOutput()
            try:
                async with closing_async_model_source(self.stream_complete_events(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    thinking=thinking,
                )) as source:
                    async for event in source:
                        if event.kind == "finish":
                            finish_reason = event.delta or None
                            continue
                        if event.kind == "usage":
                            usage = event.usage
                            continue
                        if not first_token_received and event.kind in {"content", "reasoning"}:
                            first_token_received = True
                            raw_first = (_stream_received.get() or {}).get("first")
                            ttft_ms = round(((raw_first[1] if raw_first else perf_counter()) - stream_started) * 1000, 3)
                            update_langfuse_generation(
                                generation,
                                completion_start_time=raw_first[2] if raw_first else datetime.now(UTC),
                                metadata={
                                    **generation_metadata,
                                    "first_token_received": True,
                                    "first_token_kind": event.kind,
                                    "ttft_ms": ttft_ms,
                                },
                            )
                        if event.kind == "content":
                            content_parts.append(event.delta)
                        elif event.kind == "reasoning":
                            reasoning_parts.append(event.delta)
                        for checked in callback_buffer.add(event):
                            on_delta(checked)
            except LlmProviderError as exc:
                received = _stream_received.get() or {}
                raw_first = received.get("first")
                first_token_received = bool(raw_first)
                if raw_first:
                    ttft_ms = round((raw_first[1] - stream_started) * 1000, 3)
                update_langfuse_generation(
                    generation,
                    level="ERROR",
                    status_message=_error_status_message(
                        exc,
                        first_token_received=first_token_received,
                    ),
                    metadata={
                        **generation_metadata,
                        "first_token_received": first_token_received,
                        "content_chars": received.get("content_chars", 0),
                        "reasoning_chars": received.get("reasoning_chars", 0),
                        "timeout_phase": exc.timeout_phase,
                        **({"ttft_ms": ttft_ms} if ttft_ms is not None else {}),
                    },
                )
                raise
            result = LlmCompletionResult(
                content="".join(content_parts),
                reasoning_content="".join(reasoning_parts),
                model=_stream_response_model.get() or self._model,
                provider=self.provider,
                usage=usage,
                finish_reason=finish_reason,
            )
            result = protect_completion(result)
            if result.finish_reason != "length" and callback_buffer.count:
                for checked in callback_buffer.finish():
                    on_delta(checked)
            _record_langfuse_result(
                generation,
                result=result,
                metadata={
                    **generation_metadata,
                    "first_token_received": first_token_received,
                    "content_chars": len(result.content),
                    "reasoning_chars": len(result.reasoning_content),
                    "finish_reason": finish_reason,
                    **({"ttft_ms": ttft_ms} if ttft_ms is not None else {}),
                },
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
        system_prompt, user_prompt = protect_model_prompts(system_prompt, user_prompt)
        _stream_response_model.set(None)
        _stream_received.set({"first": None, "content_chars": 0, "reasoning_chars": 0})
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
        generation_metadata = _generation_request_metadata(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            request_mode="streaming",
            timeout_seconds=self._timeout_seconds,
        )
        with langfuse_generation(
            name="deepseek.chat.completions",
            model=self._model,
            model_parameters={
                "temperature": temperature,
                "max_tokens": max_tokens,
                "thinking": thinking,
            },
            metadata=generation_metadata,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        ) as generation:
            stream_started = perf_counter()
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            finish_reason: str | None = None
            usage: dict[str, int] = {}
            first_token_received = False
            ttft_ms: float | None = None
            callback_buffer = IncrementalOutput()
            try:
                with closing_model_source(self._stream_events_sync(payload)) as source:
                    for event in source:
                        if event.kind == "finish":
                            finish_reason = event.delta or None
                            continue
                        if event.kind == "usage":
                            usage = event.usage
                            continue
                        if not first_token_received and event.kind in {"content", "reasoning"}:
                            first_token_received = True
                            raw_first = (_stream_received.get() or {}).get("first")
                            ttft_ms = round(((raw_first[1] if raw_first else perf_counter()) - stream_started) * 1000, 3)
                            update_langfuse_generation(
                                generation,
                                completion_start_time=raw_first[2] if raw_first else datetime.now(UTC),
                                metadata={
                                    **generation_metadata,
                                    "first_token_received": True,
                                    "first_token_kind": event.kind,
                                    "ttft_ms": ttft_ms,
                                },
                            )
                        if event.kind == "content":
                            content_parts.append(event.delta)
                        elif event.kind == "reasoning":
                            reasoning_parts.append(event.delta)
                        for checked in callback_buffer.add(event):
                            on_delta(checked)
            except LlmProviderError as exc:
                received = _stream_received.get() or {}
                raw_first = received.get("first")
                first_token_received = bool(raw_first)
                if raw_first:
                    ttft_ms = round((raw_first[1] - stream_started) * 1000, 3)
                update_langfuse_generation(
                    generation,
                    level="ERROR",
                    status_message=_error_status_message(
                        exc, first_token_received=first_token_received,
                    ),
                    metadata={
                        **generation_metadata,
                        "first_token_received": first_token_received,
                        "content_chars": received.get("content_chars", 0),
                        "reasoning_chars": received.get("reasoning_chars", 0),
                        "timeout_phase": exc.timeout_phase,
                        **({"ttft_ms": ttft_ms} if ttft_ms is not None else {}),
                    },
                )
                raise
            result = LlmCompletionResult(
                content="".join(content_parts),
                reasoning_content="".join(reasoning_parts),
                model=_stream_response_model.get() or self._model,
                provider=self.provider,
                usage=usage,
                finish_reason=finish_reason,
            )
            result = protect_completion(result)
            if result.finish_reason != "length" and callback_buffer.count:
                for checked in callback_buffer.finish():
                    on_delta(checked)
            _record_langfuse_result(
                generation,
                result=result,
                metadata={
                    **generation_metadata,
                    "first_token_received": first_token_received,
                    "content_chars": len(result.content),
                    "reasoning_chars": len(result.reasoning_content),
                    "finish_reason": finish_reason,
                    **({"ttft_ms": ttft_ms} if ttft_ms is not None else {}),
                },
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

    def _stream_events_with_sync_client(self, client, payload):
        failure, buffer = None, IncrementalOutput()
        try:
            with bounded_stream(client, "POST", "/chat/completions", json=payload,
                budget_kind="model",
                timeout_seconds=self._timeout_seconds,
                headers={"Authorization": f"Bearer {self._api_key}"}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    _record_packet_model(line)
                    for event in _parse_openai_stream_events(line):
                        _mark_received(event)
                        yield from buffer.add(event)
        except PROVIDER_TRANSPORT_ERRORS as error:
            failure = normalized_provider_error(error)
        if failure is not None:
            raise failure
        yield from buffer.finish()

    def close(self) -> None:
        self._sdk_async_client.retire()
        self._sdk_sync_client.close()
        if self._owns_sync_http_client and self._sync_http_client is not None:
            self._sync_http_client.close()
        self._sync_http_client = None

    async def aclose(self) -> None:
        try:
            await self._sdk_async_client.aclose()
        finally:
            self.close()

    async def _stream_events_with_client(self, client, payload):
        failure, buffer = None, IncrementalOutput()
        try:
            async with async_bounded_stream(client, "POST", "/chat/completions", json=payload,
                budget_kind="model",
                timeout_seconds=self._timeout_seconds,
                headers={"Authorization": f"Bearer {self._api_key}"}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    _record_packet_model(line)
                    for event in _parse_openai_stream_events(line):
                        _mark_received(event)
                        for checked in buffer.add(event):
                            yield checked
        except PROVIDER_TRANSPORT_ERRORS as error:
            failure = normalized_provider_error(error)
        if failure is not None:
            raise failure
        for event in buffer.finish():
            yield event


def _record_packet_model(line):
    invalid = False
    try:
        packet_model = json.loads(line.removeprefix("data:").strip()).get("model")
    except RecursionError:
        invalid = True
    except (TypeError, ValueError, AttributeError):
        return
    if invalid:
        raise LlmProviderError(code="llm_invalid_response")
    if isinstance(packet_model, str):
        if output_view(packet_model) != packet_model:
            raise ModelOutputRejected
        _stream_response_model.set(packet_model)


def _parse_openai_stream_events(line: str) -> Iterator[LlmStreamDelta]:
    if not line.startswith("data:"):
        return
    data = line.removeprefix("data:").strip()
    if not data or data == "[DONE]":
        return
    invalid = False
    try:
        payload = json.loads(data)
    except RecursionError:
        invalid = True
    except (TypeError, json.JSONDecodeError):
        return
    if invalid:
        raise LlmProviderError(code="llm_invalid_response")
    if not isinstance(payload, dict):
        return
    usage = payload.get("usage")
    if isinstance(usage, dict):
        normalized_usage = {
            key: value
            for key, value in usage.items()
            if isinstance(key, str)
            and (
                isinstance(value, int)
                or (key.endswith("_cost") and isinstance(value, (int, float)))
            )
            and not isinstance(value, bool)
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


def _generation_request_metadata(
    *,
    system_prompt: str,
    user_prompt: str,
    request_mode: str,
    timeout_seconds: float,
) -> dict[str, str | int | float]:
    return {
        "request_mode": request_mode,
        "system_prompt_chars": len(system_prompt),
        "user_prompt_chars": len(user_prompt),
        "input_chars": len(system_prompt) + len(user_prompt),
        "timeout_seconds": timeout_seconds,
    }


def _record_langfuse_result(
    generation: Any | None,
    *,
    result: LlmCompletionResult,
    metadata: dict[str, Any],
) -> None:
    attributes: dict[str, Any] = {"metadata": metadata, "model": result.model}
    attributes["output"] = {
        "content": result.content,
        "reasoning_content": result.reasoning_content,
        "finish_reason": result.finish_reason,
    }
    usage_details = _langfuse_usage_details(result.usage)
    if usage_details:
        attributes["usage_details"] = usage_details
    cost_details = _langfuse_cost_details(result.usage)
    if cost_details:
        attributes["cost_details"] = cost_details
    update_langfuse_generation(generation, **attributes)


def _timeout_phase(error: httpx.TimeoutException) -> str:
    if isinstance(error, httpx.ConnectTimeout):
        return "connect"
    if isinstance(error, httpx.ReadTimeout):
        return "read"
    if isinstance(error, httpx.WriteTimeout):
        return "write"
    if isinstance(error, httpx.PoolTimeout):
        return "pool"
    return "unknown"


def _error_status_message(
    error: LlmProviderError,
    *,
    first_token_received: bool | None = None,
) -> str:
    parts = [error.code]
    if error.timeout_phase:
        parts.append(error.timeout_phase)
    if first_token_received is not None:
        parts.append("after_first_token" if first_token_received else "before_first_token")
    return ":".join(parts)


def _langfuse_usage_details(usage: dict[str, Any]) -> dict[str, int]:
    mapping = {
        "prompt_tokens": "input",
        "completion_tokens": "output",
        "total_tokens": "total",
    }
    return {
        target: value
        for source, target in mapping.items()
        if isinstance((value := usage.get(source)), int)
        and not isinstance(value, bool)
    }


def _langfuse_cost_details(usage: dict[str, Any]) -> dict[str, float]:
    mapping = {
        "input_cost": "input",
        "prompt_cost": "input",
        "output_cost": "output",
        "completion_cost": "output",
        "total_cost": "total",
    }
    return {
        target: float(value)
        for source, target in mapping.items()
        if isinstance((value := usage.get(source)), (int, float))
        and not isinstance(value, bool)
    }
