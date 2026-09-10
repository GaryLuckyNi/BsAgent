import httpx

from app.skills.definitions import SkillCard, SkillDefinition, SkillExecutionGuide
from app.skills.registry_client import (
    SkillDefinitionProvider,
    SkillDefinitionProviderError,
)


class SkillRegistryUnavailableError(RuntimeError):
    code = "skill_registry_unavailable"
    http_status = 503


class SkillRegistry:
    def __init__(self, definition_provider: SkillDefinitionProvider | None = None) -> None:
        self._definition_provider = definition_provider

    def discover(
        self,
        *,
        domain: str | None = None,
        subdomain: str | None = None,
    ) -> list[SkillCard]:
        if self._definition_provider is None:
            cards = []
        else:
            try:
                cards = self._definition_provider.list_cards()
            except _PROVIDER_ERRORS as exc:
                raise SkillRegistryUnavailableError(
                    "Skill registry is unavailable."
                ) from exc
        return [
            card
            for card in cards
            if (domain is None or not card.domains or domain in card.domains)
            and (
                subdomain is None
                or not card.subdomains
                or subdomain in card.subdomains
            )
        ]

    def get_definition_layers(
        self,
        *,
        name: str,
    ) -> tuple[SkillDefinition, SkillExecutionGuide]:
        if self._definition_provider is None:
            raise RuntimeError("No skill definition provider configured.")
        try:
            return (
                self._definition_provider.get_definition(name=name),
                self._definition_provider.get_execution_guide(name=name),
            )
        except _PROVIDER_ERRORS as exc:
            raise SkillRegistryUnavailableError(
                "Skill registry is unavailable."
            ) from exc

_PROVIDER_ERRORS = (
    httpx.RequestError,
    httpx.HTTPStatusError,
    SkillDefinitionProviderError,
)
