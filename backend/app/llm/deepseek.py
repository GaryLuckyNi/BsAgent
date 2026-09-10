from typing import Any
from collections.abc import AsyncIterator
import json

import httpx
from langchain_openai import ChatOpenAI

from app.llm.schemas import LlmCompletionResult


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
    ) -> None:
        if not api_key:
            raise LlmConfigurationError("DeepSeek api key is required.")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client
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
        return self._chat_model.bind_tools(tools, tool_choice=tool_choice)

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
            payload["thinking"] = {
                "type": "enabled" if thinking else "disabled"
            }

        if self._http_client is not None:
            return await self._complete_with_client(self._http_client, payload)

        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
        ) as client:
            return await self._complete_with_client(client, payload)

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
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmProviderError(
                code="llm_invalid_response",
                message="DeepSeek response did not contain choices[0].message.content.",
            ) from exc

        return LlmCompletionResult(
            content=content,
            model=self._model,
            provider=self.provider,
            usage=data.get("usage", {}),
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
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if thinking is not None:
            payload["thinking"] = {
                "type": "enabled" if thinking else "disabled"
            }

        if self._http_client is not None:
            async for chunk in self._stream_with_client(self._http_client, payload):
                yield chunk
            return

        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
        ) as client:
            async for chunk in self._stream_with_client(client, payload):
                yield chunk

    async def _stream_with_client(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
    ) -> AsyncIterator[str]:
        try:
            async with client.stream(
                "POST",
                "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    chunk = _parse_openai_stream_line(line)
                    if chunk:
                        yield chunk
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


def _parse_openai_stream_line(line: str) -> str | None:
    if not line.startswith("data:"):
        return None
    data = line.removeprefix("data:").strip()
    if not data or data == "[DONE]":
        return None
    try:
        payload = json.loads(data)
        content = payload["choices"][0].get("delta", {}).get("content")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(content, str) or not content:
        return None
    return content
