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
    def list_cards(self, *, tenant_id: str) -> list[SkillCard]:
        raise NotImplementedError

    def get_definition(self, *, name: str, version: str) -> SkillDefinition:
        raise NotImplementedError

    def get_execution_guide(self, *, name: str, version: str) -> SkillExecutionGuide:
        raise NotImplementedError


class RemoteSkillRegistryClient:
    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def list_cards(self, *, tenant_id: str) -> list[SkillCard]:
        response = self._client.get("/skills/cards", params={"tenant_id": tenant_id})
        response.raise_for_status()
        payload = response.json()
        return [SkillCard.model_validate(item) for item in payload.get("items", [])]

    def get_definition(self, *, name: str, version: str) -> SkillDefinition:
        response = self._client.get(f"/skills/{name}/versions/{version}/definition")
        response.raise_for_status()
        return SkillDefinition.model_validate(response.json())

    def get_execution_guide(self, *, name: str, version: str) -> SkillExecutionGuide:
        response = self._client.get(f"/skills/{name}/versions/{version}/execution-guide")
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
        self._cards_cache: dict[str, tuple[float, list[SkillCard]]] = {}
        self._definition_cache: dict[tuple[str, str], tuple[float, SkillDefinition]] = {}
        self._guide_cache: dict[tuple[str, str], tuple[float, SkillExecutionGuide]] = {}

    def list_cards(self, *, tenant_id: str) -> list[SkillCard]:
        cached = self._cards_cache.get(tenant_id)
        if cached is not None and not self._is_expired(cached[0]):
            return cached[1]
        cards = self._provider.list_cards(tenant_id=tenant_id)
        self._cards_cache[tenant_id] = (self._now(), cards)
        return cards

    def get_definition(self, *, name: str, version: str) -> SkillDefinition:
        key = (name, version)
        cached = self._definition_cache.get(key)
        if cached is not None and not self._is_expired(cached[0]):
            return cached[1]
        definition = self._provider.get_definition(name=name, version=version)
        self._definition_cache[key] = (self._now(), definition)
        return definition

    def get_execution_guide(self, *, name: str, version: str) -> SkillExecutionGuide:
        key = (name, version)
        cached = self._guide_cache.get(key)
        if cached is not None and not self._is_expired(cached[0]):
            return cached[1]
        guide = self._provider.get_execution_guide(name=name, version=version)
        self._guide_cache[key] = (self._now(), guide)
        return guide

    def _is_expired(self, cached_at: float) -> bool:
        return self._now() - cached_at >= self._ttl_seconds
