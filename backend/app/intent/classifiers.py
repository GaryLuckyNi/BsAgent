import asyncio
import json
import re

from app.intent.catalog import BusinessIntentCatalog
from app.intent.schemas import (
    BusinessIntentProfile,
    IntentDecisionModel,
    IntentRoutingResult,
    IntentSignal,
)
from app.llm.factory import LlmClient
from app.skills.definitions import SkillCard


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
        available_skills: list[SkillCard] | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._catalog = catalog
        self._available_skills = available_skills or []

    def classify(
        self,
        *,
        message: str,
        signal: IntentSignal,
        context: dict | None = None,
        matched_profile: BusinessIntentProfile | None = None,
    ) -> IntentRoutingResult:
        result = asyncio.run(
            self._llm_client.complete(
                system_prompt=(
                    "你是 BsAgent 的投研优先意图分类器。只能输出 JSON object，不要输出解释。"
                    "顶层 intent 必须是允许值之一。domain/sub_intent 是业务语义标签。"
                    "A股公司研究、财务、行情、估值、公告、风险与对比问题应归入"
                    "domain=a_share_equity_research，并由投研工作流处理。"
                    "寒暄、产品帮助和简单金融通识可以归入 conversation。"
                    "明显与投研无关的专业任务应输出 intent=unsupported、domain=out_of_scope，"
                    "由回答层说明能力边界，不要伪装成对应领域专家。"
                    "Intent 层只判断用户目标、业务领域、子意图和候选 Skill，"
                    "不负责选择工具，candidate_tool 必须输出 null。"
                    "同一次请求必须输出 decision 和 task_brief。"
                    "task_workflow 必须生成 task_brief；寒暄、帮助、unsupported 等"
                    "非任务路由的 task_brief 输出 null。"
                    "Admin Profile 只作业务提示，不能代替理解本次用户目标、实体和时效需求。"
                    "不要为开放式投资决策问法创造新的 sub_intent；"
                    "未命中 Profile 的 A 股投研任务允许 sub_intent=null。"
                    "TaskBrief 只提取用户明确提及或上下文唯一确定的实体，"
                    "search_keywords 必须包含核心实体；涉及当前、近期或最新公开动态时，"
                    "freshness_required 和 web_search_required 应为 true。"
                    "candidate_skill 只能从 available_skills 的 name 中选择；"
                    "没有匹配 Skill 时填写 null。输出必须是单行紧凑 JSON，不要使用 Markdown。"
                ),
                user_prompt=json.dumps(
                    {
                        "message": message,
                        "context": context or {},
                        "rule_signal": signal.model_dump(),
                        "matched_profile": (
                            matched_profile.model_dump() if matched_profile else None
                        ),
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
                        "available_skills": [
                            {
                                "name": skill.name,
                                "version": skill.version,
                                "summary": skill.summary,
                                "supported_intents": skill.supported_intents,
                                "tags": skill.tags,
                                "risk_level": skill.risk_level,
                            }
                            for skill in self._available_skills
                        ],
                        "required_schema": {
                            "decision": {
                                "intent": "string",
                                "domain": "string|null",
                                "sub_intent": "string|null",
                                "candidate_skill": "string|null",
                                "candidate_tool": "null",
                                "risk_level": "low|medium|high",
                                "confidence": "number between 0 and 1",
                                "needs_clarification": "boolean",
                                "reason_codes": "string[]",
                            },
                            "task_brief": {
                                "user_goal": "string",
                                "entities": (
                                    "[{type: company|industry|security|document, "
                                    "mention: string}]"
                                ),
                                "analysis_questions": "string[]",
                                "search_keywords": "string[]",
                                "freshness_required": "boolean",
                                "web_search_required": "boolean",
                                "web_search_reason": "string|null",
                                "rag_required": "boolean",
                                "time_horizon": "string|null",
                                "output_expectation": "string",
                                "constraints": "string[]",
                                "needs_clarification": "boolean",
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                temperature=0.0,
                max_tokens=1536,
                thinking=False,
            )
        )
        payload = _extract_json_object(result.content)
        return IntentRoutingResult.model_validate(_normalize_routing_payload(payload))


def _normalize_routing_payload(payload: dict) -> dict:
    if "decision" in payload:
        return payload
    return {"decision": payload, "task_brief": None}


def _extract_json_object(content: str) -> dict:
    stripped = content.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match is None:
        raise ValueError("LLM intent output did not contain JSON object.")
    return json.loads(match.group(0))
