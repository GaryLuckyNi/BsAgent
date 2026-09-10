import hashlib
import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.skills.definitions import (
    SkillCard,
    SkillDefinition,
    SkillEvidenceContract,
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
    allowed_tools: list[str]
    evidence_contract: Any = None
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
        response = self._client.get("/api/skills", params={"enabled": "true"})
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
        evidence_contract, evidence_contract_error = _parse_evidence_contract(item)
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
            allowed_tools=item.allowed_tools,
            constraints=["遵守 Skill 配置的 allowed_tools 和 risk_level"],
            failure_modes=[
                "missing_required_tool",
                "tool_execution_failed",
                "insufficient_evidence",
            ],
            risk_level=item.risk_level,
            evidence_contract=evidence_contract,
            evidence_contract_error=evidence_contract_error,
            definition_hash=_definition_hash(item),
        )

    def get_execution_guide(self, *, name: str) -> SkillExecutionGuide:
        item = self._get_skill(name)
        return SkillExecutionGuide(
            name=item.name,
            version=item.version,
            instructions=[
                "根据用户问题和 intent 生成 allowed_tools 中的工具调用请求。",
                "结构化金融数据只能通过 MCP stock tools 获取。",
                "非结构化证据通过 rag_search 获取。",
            ],
            few_shot_examples=[],
            recovery_policy=["工具不可用时降级说明", "证据不足时明确不确定性"],
            required_evidence_policy="answer_prefers_cited_evidence",
            definition_hash=_definition_hash(item),
        )

    def _get_skill(self, name: str) -> _AdminSkillPayload:
        response = self._client.get(f"/api/skills/{name}")
        response.raise_for_status()
        item = _validate_response_payload(response, _AdminSkillPayload)
        if item.enabled is False:
            raise ValueError(f"Skill is disabled: {name}")
        return item


def _definition_hash(payload: _AdminSkillPayload) -> str:
    payload = payload.model_dump(mode="json", exclude_unset=True)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_evidence_contract(
    item: _AdminSkillPayload,
) -> tuple[SkillEvidenceContract, str | None]:
    if "evidence_contract" not in item.model_fields_set:
        return SkillEvidenceContract(), None
    try:
        return SkillEvidenceContract.model_validate(item.evidence_contract), None
    except ValidationError:
        return SkillEvidenceContract(), "skill_evidence_contract_invalid"


def _validate_response_payload(response: httpx.Response, model_type: type[BaseModel]):
    try:
        payload = response.json()
        return model_type.model_validate(payload)
    except (ValueError, ValidationError):
        raise SkillDefinitionProviderError() from None
