import asyncio
import json
import re

from app.intent.catalog import BusinessIntentCatalog
from app.intent.schemas import IntentDecisionModel, IntentSignal
from app.llm.factory import LlmClient
from app.schemas.tools import ToolSpec


class RuleOnlyIntentClassifier:
    def __init__(self, *, catalog: BusinessIntentCatalog) -> None:
        self._catalog = catalog

    def classify(self, *, message: str, signal: IntentSignal) -> IntentDecisionModel:
        if signal.hard_intent is not None:
            return IntentDecisionModel(
                intent=signal.hard_intent,
                risk_level=signal.risk_floor,
                confidence=0.9,
                needs_clarification=signal.needs_clarification,
                reason_codes=signal.reason_codes,
            )

        profile = self._catalog.match_by_keywords(message)
        if profile is not None:
            return IntentDecisionModel(
                intent=profile.intent,
                domain=profile.domain,
                sub_intent=profile.sub_intent,
                candidate_skill=profile.candidate_skill,
                candidate_tool=profile.candidate_tool,
                risk_level=profile.risk_level,
                confidence=0.78,
                reason_codes=[
                    *signal.reason_codes,
                    f"business_profile:{profile.id}",
                ],
            )

        return IntentDecisionModel(
            intent="knowledge_qa",
            risk_level=signal.risk_floor,
            confidence=0.55,
            needs_clarification=False,
            reason_codes=[*signal.reason_codes, "rule_default_knowledge_qa"],
        )


class LlmIntentClassifier:
    def __init__(
        self,
        *,
        llm_client: LlmClient,
        catalog: BusinessIntentCatalog,
        available_tools: list[ToolSpec] | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._catalog = catalog
        self._available_tools = available_tools or []

    def classify(self, *, message: str, signal: IntentSignal) -> IntentDecisionModel:
        result = asyncio.run(
            self._llm_client.complete(
                system_prompt=(
                    "你是 BsAgent 的意图分类器。只能输出 JSON object，不要输出解释。"
                    "顶层 intent 必须是允许值之一。domain/sub_intent 是业务语义标签。"
                    "如果 available_tools 中存在能完成用户目标的工具，优先输出 intent=tool_action "
                    "并填写 candidate_tool。输出必须是单行紧凑 JSON，不要使用 Markdown。"
                ),
                user_prompt=json.dumps(
                    {
                        "message": message,
                        "rule_signal": signal.model_dump(),
                        "allowed_intents": [
                            "knowledge_qa",
                            "memory_query",
                            "tool_action",
                            "task_workflow",
                            "conversation",
                            "clarification_needed",
                            "unsupported",
                        ],
                        "business_profiles": [
                            profile.model_dump()
                            for profile in self._catalog.all_profiles()
                        ],
                        "available_tools": [
                            {
                                "name": tool.name,
                                "version": tool.version,
                                "description": tool.description,
                                "input_schema": tool.input_schema,
                                "risk_level": tool.risk_level,
                                "permission_scope": tool.permission_scope,
                            }
                            for tool in self._available_tools
                        ],
                        "required_schema": {
                            "intent": "string",
                            "domain": "string|null",
                            "sub_intent": "string|null",
                            "candidate_skill": "string|null",
                            "candidate_tool": "string|null",
                            "risk_level": "low|medium|high",
                            "confidence": "number between 0 and 1",
                            "needs_clarification": "boolean",
                            "reason_codes": "string[]",
                        },
                    },
                    ensure_ascii=False,
                ),
                temperature=0.0,
                max_tokens=768,
            )
        )
        return IntentDecisionModel.model_validate(_extract_json_object(result.content))


def _extract_json_object(content: str) -> dict:
    stripped = content.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match is None:
        raise ValueError("LLM intent output did not contain JSON object.")
    return json.loads(match.group(0))
