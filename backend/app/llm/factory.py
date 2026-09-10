from collections.abc import AsyncIterator
from typing import Any, Protocol

from app.core.config import settings
from app.llm.deepseek import DeepSeekClient, LlmConfigurationError
from app.llm.schemas import LlmCompletionResult


class LlmClient(Protocol):
    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool | None = None,
    ) -> LlmCompletionResult:
        ...

    async def stream_complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 256,
        thinking: bool | None = None,
    ) -> AsyncIterator[str]:
        ...


class ToolCallingLlmClient(LlmClient, Protocol):
    def bind_tools(self, tools: list[dict[str, Any]], *, tool_choice: str): ...


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
