from typing import Any

import httpx

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

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
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
