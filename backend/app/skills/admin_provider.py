import hashlib
import json
from typing import Any

import httpx

from app.skills.definitions import SkillCard, SkillDefinition, SkillExecutionGuide


class AdminSkillDefinitionProvider:
    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def list_cards(self, *, tenant_id: str) -> list[SkillCard]:
        response = self._client.get("/api/skills", params={"enabled": "true"})
        response.raise_for_status()
        items = response.json().get("items", [])
        return [
            SkillCard(
                name=item["name"],
                version=item["version"],
                summary=item["summary"],
                supported_intents=item.get("supported_intents", []),
                tags=[
                    *item.get("supported_domains", []),
                    *item.get("supported_sub_intents", []),
                ],
                risk_level=item.get("risk_level", "medium"),
                definition_hash=_definition_hash(item),
            )
            for item in items
        ]

    def get_definition(self, *, name: str, version: str) -> SkillDefinition:
        item = self._get_skill(name)
        return SkillDefinition(
            name=item["name"],
            version=item["version"],
            description=item["summary"],
            input_contract={
                "supported_intents": item.get("supported_intents", []),
                "supported_domains": item.get("supported_domains", []),
                "supported_sub_intents": item.get("supported_sub_intents", []),
            },
            output_contract={"type": "object"},
            allowed_tools=item.get("allowed_tools", []),
            constraints=["遵守 Skill 配置的 allowed_tools 和 risk_level"],
            failure_modes=[
                "missing_required_tool",
                "tool_execution_failed",
                "insufficient_evidence",
            ],
            definition_hash=_definition_hash(item),
        )

    def get_execution_guide(self, *, name: str, version: str) -> SkillExecutionGuide:
        item = self._get_skill(name)
        return SkillExecutionGuide(
            name=item["name"],
            version=item["version"],
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

    def _get_skill(self, name: str) -> dict[str, Any]:
        response = self._client.get(f"/api/skills/{name}")
        response.raise_for_status()
        item = response.json()
        if item.get("enabled") is False:
            raise ValueError(f"Skill is disabled: {name}")
        return item


def _definition_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
