from time import monotonic
from typing import Callable, Protocol

import httpx

from app.skills.definitions import SkillCard, SkillDefinition, SkillExecutionGuide


class SkillDefinitionProviderError(RuntimeError):
    code = "skill_registry_payload_invalid"
    http_status = 503

    def __init__(self) -> None:
        super().__init__("Skill 注册中心返回无效响应。")


class SkillDefinitionProvider(Protocol):
    def list_cards(self) -> list[SkillCard]:
        raise NotImplementedError

    def get_definition(self, *, name: str) -> SkillDefinition:
        raise NotImplementedError

    def get_execution_guide(self, *, name: str) -> SkillExecutionGuide:
        raise NotImplementedError


class RemoteSkillRegistryClient:
    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def list_cards(self) -> list[SkillCard]:
        response = self._client.get("/skills/cards")
        response.raise_for_status()
        payload = response.json()
        return [SkillCard.model_validate(item) for item in payload.get("items", [])]

    def get_definition(self, *, name: str) -> SkillDefinition:
        response = self._client.get(f"/skills/{name}/definition")
        response.raise_for_status()
        return SkillDefinition.model_validate(response.json())

    def get_execution_guide(self, *, name: str) -> SkillExecutionGuide:
        response = self._client.get(f"/skills/{name}/execution-guide")
        response.raise_for_status()
        return SkillExecutionGuide.model_validate(response.json())


class CachedSkillDefinitionProvider:
    def __init__(
        self,
        provider: SkillDefinitionProvider,
        *,
        ttl_seconds: int,
        now: Callable[[], float] = monotonic,
    ) -> None:
        self._provider = provider
        self._ttl_seconds = ttl_seconds
        self._now = now
        self._cards_cache: tuple[float, list[SkillCard]] | None = None
        self._definition_cache: dict[str, tuple[float, SkillDefinition]] = {}
        self._guide_cache: dict[str, tuple[float, SkillExecutionGuide]] = {}

    def list_cards(self) -> list[SkillCard]:
        cached = self._cards_cache
        if cached is not None and not self._is_expired(cached[0]):
            return cached[1]
        cards = self._provider.list_cards()
        self._cards_cache = (self._now(), cards)
        return cards

    def get_definition(self, *, name: str) -> SkillDefinition:
        cached = self._definition_cache.get(name)
        if cached is not None and not self._is_expired(cached[0]):
            return cached[1]
        definition = self._provider.get_definition(name=name)
        self._definition_cache[name] = (self._now(), definition)
        return definition

    def get_execution_guide(self, *, name: str) -> SkillExecutionGuide:
        cached = self._guide_cache.get(name)
        if cached is not None and not self._is_expired(cached[0]):
            return cached[1]
        guide = self._provider.get_execution_guide(name=name)
        self._guide_cache[name] = (self._now(), guide)
        return guide

    def _is_expired(self, cached_at: float) -> bool:
        return self._now() - cached_at >= self._ttl_seconds
