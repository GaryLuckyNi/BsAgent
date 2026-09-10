import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

from langgraph.runtime import Runtime

from app.graph.planning.contracts import PlanModel, TaskResultModel
from app.graph.state import AgentState
from app.llm.factory import LlmClient
from app.memory.preferences import (
    presentation_preference_constraints,
    recall_user_preferences,
)


FINANCIAL_DISCLAIMER = "以上内容仅用于投研辅助分析，不构成投资建议、买卖建议或收益承诺。"
OUT_OF_SCOPE_RESPONSE = (
    "当前问题不在本服务已启用的投研范围内。你可以围绕公司、行业、财务、行情、"
    "公告或投资风险提出问题。"
)
_MODEL_BRAND_PATTERN = re.compile(
    r"(?:OpenAI|ChatGPT|Claude|Anthropic|Gemini|DeepSeek|Qwen|通义千问|豆包|Kimi)",
    re.IGNORECASE,
)


def build_synthesizer_node(
    *,
    llm_client: LlmClient | None = None,
    on_memory_recall_failure: Callable[[AgentState], None] | None = None,
):
    def node(state: AgentState, runtime: Runtime | None = None) -> AgentState:
        handled_error = next(
            (error for error in reversed(state["errors"]) if error.get("handled")),
            None,
        )
        if handled_error is not None:
            state["draft_answer"] = _draft(
                str(handled_error.get("message") or "请求执行失败，请稍后重试。"),
                response_type="execution_error_response",
                business_error_code=str(
                    handled_error.get("code") or "agent_execution_failed"
                ),
            )
            return state
        intent = state.get("intent") or {}
        if not intent.get("intent_type"):
            state["draft_answer"] = _draft(OUT_OF_SCOPE_RESPONSE, response_type="out_of_scope")
            return state

        plan = _validated_plan(state)
        results = _current_results(state, plan)
        failures = [result for result in results if result.status != "success"]
        if failures:
            content = _failure_response(failures)
            state["draft_answer"] = _draft(
                content,
                response_type="execution_error_response",
                business_error_code=failures[0].error_code,
            )
            return state

        preferences = presentation_preference_constraints(
            recall_user_preferences(
                runtime,
                state,
                on_failure=on_memory_recall_failure,
            )
        )
        content = _render_answer(
            state=state,
            goal=str(intent.get("goal") or state["user_message"]),
            results=results,
            preferences=preferences,
            llm_client=llm_client,
        )
        state["draft_answer"] = _draft(
            _ensure_financial_disclaimer(_strip_model_brand(content)),
            response_type="research_answer",
            confidence=1.0 if results else 0.5,
            response_policy=preferences,
        )
        return state

    return node


def _validated_plan(state: AgentState) -> PlanModel | None:
    raw_plan = state.get("plan")
    return PlanModel.model_validate(raw_plan) if raw_plan else None


def _current_results(
    state: AgentState,
    plan: PlanModel | None,
) -> list[TaskResultModel]:
    task_ids = {task.id for task in plan.tasks} if plan else set()
    return [
        result
        for item in state.get("task_results", [])
        for result in [TaskResultModel.model_validate(item)]
        if not task_ids or result.task_id in task_ids
    ]


def _render_answer(
    *,
    state: AgentState,
    goal: str,
    results: list[TaskResultModel],
    preferences: dict[str, Any],
    llm_client: LlmClient | None,
) -> str:
    if llm_client is None:
        return _fallback_answer(goal, results)
    completion = asyncio.run(
        llm_client.complete(
            system_prompt=(
                "你是 BsAgent 的投研报告合成器。直接回答用户目标，使用清晰、自然、专业的"
                "中文 Markdown，根据问题本身决定章节，不套用固定模板。只能使用 task_results"
                "和 evidence 中提供的信息；区分事实、推断与数据缺口，不得补造数字、来源或"
                "实时信息。避免逐字段机械复述，应解释数据之间的关系、影响与不确定性。"
                "不得冒充模型厂商，不得给出确定性收益承诺或直接买卖指令。"
            ),
            user_prompt=json.dumps(
                {
                    "goal": goal,
                    "task_results": [
                        result.model_dump(mode="json") for result in results
                    ],
                    "evidence": _evidence_context(state),
                    "presentation_preferences": preferences,
                },
                ensure_ascii=False,
            ),
            temperature=0.2,
            max_tokens=2_048,
            thinking=False,
        )
    )
    return completion.content.strip() or _fallback_answer(goal, results)


def _evidence_context(state: AgentState) -> list[dict[str, Any]]:
    evidence = state.get("evidence")
    if evidence is None:
        return []
    return [
        {
            "evidence_id": item.evidence_id,
            "title": item.title,
            "content": item.content,
            "source_uri": item.source_uri,
        }
        for item in evidence.items
    ]


def _fallback_answer(goal: str, results: list[TaskResultModel]) -> str:
    sections = [f"## 分析目标\n\n{goal}"]
    sections.extend(
        f"## {result.task_id}\n\n{result.content}" for result in results
    )
    return "\n\n".join(sections)


def _failure_response(failures: list[TaskResultModel]) -> str:
    details = "\n".join(f"- {result.content}" for result in failures)
    return f"## 本次执行未完成\n\n{details}"


def _ensure_financial_disclaimer(content: str) -> str:
    if "不构成投资建议" in content:
        return content
    return f"{content.rstrip()}\n\n## 免责声明\n\n{FINANCIAL_DISCLAIMER}"


def _strip_model_brand(content: str) -> str:
    return _MODEL_BRAND_PATTERN.sub("本服务", content)


def _draft(
    content: str,
    *,
    response_type: str,
    business_error_code: str | None = None,
    confidence: float = 0.0,
    response_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    draft = {
        "content": content,
        "claims": [],
        "cited_evidence_ids": [],
        "missing_information": [],
        "assumptions": [],
        "confidence": confidence,
        "response_policy": response_policy or {},
        "response_type": response_type,
    }
    if business_error_code:
        draft["business_error_code"] = business_error_code
    return draft


def synthesizer_node(state: AgentState, runtime: Runtime | None = None) -> AgentState:
    return build_synthesizer_node()(state, runtime)
