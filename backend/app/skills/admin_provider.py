import hashlib
import json
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.skills.definitions import (
    SkillCard,
    SkillDefinition,
    SkillExecutionGuide,
)
from app.skills.registry_client import SkillDefinitionProviderError


class _AdminSkillPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    version: str
    summary: str
    supported_intents: list[str]
    supported_domains: list[str]
    supported_sub_intents: list[str]
    risk_level: Literal["low", "medium", "high"]
    enabled: bool


class _AdminSkillListPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[_AdminSkillPayload]
    total: int = Field(ge=0)


class AdminSkillDefinitionProvider:
    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def list_cards(self) -> list[SkillCard]:
        response = self._client.get("/api/runtime/skills", params={"enabled": "true"})
        response.raise_for_status()
        payload = _validate_response_payload(response, _AdminSkillListPayload)
        return [
            SkillCard(
                name=item.name,
                summary=item.summary,
                domains=item.supported_domains,
                subdomains=item.supported_sub_intents,
                definition_hash=_definition_hash(item),
            )
            for item in payload.items
        ]

    def get_definition(self, *, name: str) -> SkillDefinition:
        item = self._get_skill(name)
        return SkillDefinition(
            name=item.name,
            version=item.version,
            description=item.summary,
            input_contract={
                "supported_intents": item.supported_intents,
                "supported_domains": item.supported_domains,
                "supported_sub_intents": item.supported_sub_intents,
            },
            output_contract={"type": "object"},
            constraints=["按 summary 中声明的步骤、指定工具和输出要求执行"],
            failure_modes=[
                "missing_required_tool",
                "tool_execution_failed",
                "insufficient_evidence",
            ],
            risk_level=item.risk_level,
            definition_hash=_definition_hash(item),
        )

    def get_execution_guide(self, *, name: str) -> SkillExecutionGuide:
        item = self._get_skill(name)
        return SkillExecutionGuide(
            name=item.name,
            version=item.version,
            instructions=[
                item.summary,
            ],
            few_shot_examples=[],
            recovery_policy=["工具不可用时降级说明", "证据不足时明确不确定性"],
            required_evidence_policy="answer_prefers_cited_evidence",
            definition_hash=_definition_hash(item),
        )

    def _get_skill(self, name: str) -> _AdminSkillPayload:
        response = self._client.get(f"/api/runtime/skills/{name}")
        response.raise_for_status()
        item = _validate_response_payload(response, _AdminSkillPayload)
        if item.enabled is False:
            raise ValueError(f"Skill is disabled: {name}")
        return item


def _definition_hash(payload: _AdminSkillPayload) -> str:
    payload = payload.model_dump(mode="json", exclude_unset=True)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_response_payload(response: httpx.Response, model_type: type[BaseModel]):
    try:
        payload = response.json()
        return model_type.model_validate(payload)
    except (ValueError, ValidationError):
        raise SkillDefinitionProviderError() from None
