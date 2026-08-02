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

    def discover(self, *, tenant_id: str, intent: str | None = None) -> list[SkillCard]:
        if self._definition_provider is None:
            cards = [
                SkillCard(
                    name=skill.spec.name,
                    version=skill.spec.version,
                    summary=skill.spec.description,
                    supported_intents=skill.spec.supported_intents,
                    tags=[],
                    risk_level=skill.spec.risk_level,
                    definition_hash=f"local:{skill.spec.name}:{skill.spec.version}",
                )
                for skill in self._skills.values()
            ]
        else:
            try:
                cards = self._definition_provider.list_cards(tenant_id=tenant_id)
            except _PROVIDER_ERRORS as exc:
                raise SkillRegistryUnavailableError(
                    "Skill registry is unavailable."
                ) from exc
        if intent is None:
            return cards
        return [card for card in cards if intent in card.supported_intents]

    def get_definition_layers(
        self,
        *,
        name: str,
        version: str,
    ) -> tuple[SkillDefinition, SkillExecutionGuide]:
        if self._definition_provider is None:
            raise RuntimeError("No skill definition provider configured.")
        try:
            return (
                self._definition_provider.get_definition(name=name, version=version),
                self._definition_provider.get_execution_guide(name=name, version=version),
            )
        except _PROVIDER_ERRORS as exc:
            raise SkillRegistryUnavailableError(
                "Skill registry is unavailable."
            ) from exc

    def select(self, *, intent: str, candidate_skill: str | None) -> Skill:
        if candidate_skill and candidate_skill in self._skills:
            skill = self._skills[candidate_skill]
            if intent in skill.spec.supported_intents:
                return skill
            raise KeyError(
                f"Skill {candidate_skill} does not support intent: {intent}"
            )
        if candidate_skill and self._definition_provider is not None:
            try:
                cards = self._definition_provider.list_cards(tenant_id="default")
            except _PROVIDER_ERRORS as exc:
                logger.warning(
                    "Skill definition provider is unavailable for candidate %s: %s",
                    candidate_skill,
                    exc,
                )
                fallback_skill = self._fallback_skill_for_intent(intent)
                if fallback_skill is not None:
                    return fallback_skill
                raise SkillRegistryUnavailableError(
                    f"Skill registry is unavailable for candidate: {candidate_skill}"
                ) from exc
            card = next((card for card in cards if card.name == candidate_skill), None)
            if card is not None:
                if intent not in card.supported_intents:
                    raise KeyError(
                        f"Skill {candidate_skill} does not support intent: {intent}"
                    )
                try:
                    definition = self._definition_provider.get_definition(
                        name=card.name,
                        version=card.version,
                    )
                    guide = self._definition_provider.get_execution_guide(
                        name=card.name,
                        version=card.version,
                    )
                except _PROVIDER_ERRORS as exc:
                    fallback_skill = self._fallback_skill_for_intent(intent)
                    if fallback_skill is not None:
                        return fallback_skill
                    raise SkillRegistryUnavailableError(
                        f"Skill registry is unavailable for candidate: {candidate_skill}"
                    ) from exc
                return ConfiguredSkill(
                    definition=definition,
                    guide=guide,
                    risk_level=card.risk_level,
                )
        for skill in self._skills.values():
            if intent in skill.spec.supported_intents:
                return skill
        raise KeyError(f"No skill registered for intent: {intent}")

    def _fallback_skill_for_intent(self, intent: str) -> Skill | None:
        for skill in self._skills.values():
            if intent in skill.spec.supported_intents:
                return skill
        return None


_PROVIDER_ERRORS = (
    httpx.RequestError,
    httpx.HTTPStatusError,
    SkillDefinitionProviderError,
)
