import logging

import httpx

from app.skills.configured import ConfiguredSkill
from app.skills.base import Skill
from app.skills.definitions import SkillCard, SkillDefinition, SkillExecutionGuide
from app.skills.registry_client import (
    SkillDefinitionProvider,
    SkillDefinitionProviderError,
)


logger = logging.getLogger(__name__)


class SkillRegistryUnavailableError(RuntimeError):
    code = "skill_registry_unavailable"
    http_status = 503


class SkillRegistry:
    def __init__(self, definition_provider: SkillDefinitionProvider | None = None) -> None:
        self._skills: dict[str, Skill] = {}
        self._definition_provider = definition_provider

    def register(self, skill: Skill) -> None:
        self._skills[skill.spec.name] = skill

    def discover(
        self,
        *,
        domain: str | None = None,
        subdomain: str | None = None,
    ) -> list[SkillCard]:
        if self._definition_provider is None:
            cards = [
                SkillCard(
                    name=skill.spec.name,
                    summary=skill.spec.description,
                    domains=[],
                    subdomains=[],
                    definition_hash=f"local:{skill.spec.name}:{skill.spec.version}",
                )
                for skill in self._skills.values()
            ]
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

    def select(self, *, candidate_skill: str) -> Skill:
        if candidate_skill and candidate_skill in self._skills:
            return self._skills[candidate_skill]
        if candidate_skill and self._definition_provider is not None:
            try:
                cards = self._definition_provider.list_cards()
            except _PROVIDER_ERRORS as exc:
                logger.warning(
                    "Skill definition provider is unavailable for candidate %s: %s",
                    candidate_skill,
                    exc,
                )
                raise SkillRegistryUnavailableError(
                    f"Skill registry is unavailable for candidate: {candidate_skill}"
                ) from exc
            card = next((card for card in cards if card.name == candidate_skill), None)
            if card is not None:
                try:
                    definition = self._definition_provider.get_definition(
                        name=card.name,
                    )
                    guide = self._definition_provider.get_execution_guide(
                        name=card.name,
                    )
                except _PROVIDER_ERRORS as exc:
                    raise SkillRegistryUnavailableError(
                        f"Skill registry is unavailable for candidate: {candidate_skill}"
                    ) from exc
                return ConfiguredSkill(
                    definition=definition,
                    guide=guide,
                    risk_level=definition.risk_level,
                )
        raise KeyError(f"No enabled skill registered with name: {candidate_skill}")


_PROVIDER_ERRORS = (
    httpx.RequestError,
    httpx.HTTPStatusError,
    SkillDefinitionProviderError,
)
