import json
import re
from collections.abc import Callable
from typing import Any

from langgraph.runtime import Runtime

from app.graph.planning.contracts import PlanModel, TaskResultModel
from app.graph.public_events import build_public_llm_stream_handler
from app.graph.state import AgentState
from app.llm.factory import LlmClient, invoke_llm_completion
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
                business_error_code=str(handled_error.get("code") or "agent_execution_failed"),
            )
            return state
        intent = state.get("intent") or {}
        if not intent.get("intent_type"):
            state["draft_answer"] = _draft(OUT_OF_SCOPE_RESPONSE, response_type="out_of_scope")
            return state

        plan = _validated_plan(state)
        results = _current_results(state, plan)
        failures = [result for result in results if result.status != "success"]
        successful_results = [
            result
            for result in results
            if result.status == "success" and result.tool_name != "search_tool"
        ]
        if failures and not successful_results:
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
            confidence=(
                0.65
                if failures
                or state.get("planner_stop_reason")
                or (state.get("planner_decision") or {}).get("unresolved_questions")
                else 1.0
            )
            if results
            else 0.5,
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
    research_materials, data_limitations = _public_research_context(state, results)
    if llm_client is None:
        return _fallback_answer(goal, research_materials, data_limitations)
    system_prompt = (
        "你是 BsAgent 的投研报告合成器。直接回答用户目标，使用清晰、自然、专业的"
        "中文 Markdown，根据问题本身决定章节，不套用固定模板。只能使用"
        "research_materials 和 evidence 中提供的信息；区分事实、推断与数据缺口，不得补造数字、来源或"
        "实时信息。research_assessment 是研究阶段的判断，不是新证据；保留其中证据限制，不复述内部决策标签。避免逐字段机械复述，应解释数据之间的关系、影响与不确定性。"
        "若 data_limitations 非空但已有有效材料，仍应以研究结论为主体完成力所能及的"
        "分析，仅在结论确实受影响的位置简要说明缺少哪些数据验证；不得把数据缺口"
        "扩写成全部研究失败。禁止输出执行过程、任务清单、任务 ID、工具名、内部错误码、"
        "配置修复建议或成功失败统计。不要评价研究链路本身。"
        "不得冒充模型厂商，不得给出确定性收益承诺或直接买卖指令。"
    )
    user_prompt = json.dumps(
        {
            "goal": goal,
            "research_materials": research_materials,
            "data_limitations": data_limitations,
            "research_assessment": state.get("planner_decision") or {},
            "evidence": _evidence_context(state),
            "presentation_preferences": preferences,
        },
        ensure_ascii=False,
    )
    stream_handler = build_public_llm_stream_handler(
        "synthesizer",
        state,
        stream_content=True,
    )
    try:
        completion = invoke_llm_completion(
            llm_client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=16_384,
            # 正文已由结构化研究结果约束，直接生成才能让首个正文 token
            # 立即进入流。复杂分析节点仍保留模型推理。
            thinking=False,
            on_delta=stream_handler,
            retry_without_thinking_on_empty=True,
            retry_without_thinking_on_length=False,
        )
    finally:
        stream_handler.flush()
    return completion.content.strip() or _fallback_answer(
        goal,
        research_materials,
        data_limitations,
    )


def _public_research_context(
    state: AgentState,
    results: list[TaskResultModel],
) -> tuple[list[dict[str, str]], list[str]]:
    plan = _validated_plan(state)
    task_goals = {task.id: task.task_goal for task in plan.tasks} if plan else {}
    research_materials: list[dict[str, str]] = []
    data_limitations: list[str] = list(
        (state.get("planner_decision") or {}).get("unresolved_questions") or []
    )
    for result in results:
        research_goal = task_goals.get(result.task_id, "相关投研维度")
        if result.status == "success" and result.tool_name != "search_tool":
            research_materials.append(
                {
                    "research_goal": research_goal,
                    "content": result.content,
                }
            )
        elif result.status != "success" and research_goal not in data_limitations:
            data_limitations.append(research_goal)
    completed_ids = {result.task_id for result in results}
    if plan:
        data_limitations.extend(
            task.task_goal for task in plan.tasks if task.id not in completed_ids
        )
    if state.get("planner_stop_reason") == "planner_feedback_invalid":
        data_limitations.append("后续研究步骤未能确定，部分研究目标尚未核实。")
    elif state.get("planner_stop_reason"):
        data_limitations.append("执行预算已用尽，部分研究目标尚未核实。")
    return research_materials, data_limitations


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


def _fallback_answer(
    goal: str,
    research_materials: list[dict[str, str]],
    data_limitations: list[str],
) -> str:
    sections = [f"## 分析目标\n\n{goal}"]
    sections.extend(
        f"## {material['research_goal']}\n\n{material['content']}"
        for material in research_materials
    )
    if data_limitations:
        sections.append("## 数据局限\n\n" + "\n".join(f"- {item}" for item in data_limitations))
    return "\n\n".join(sections)


def _failure_response(failures: list[TaskResultModel]) -> str:
    messages = list(dict.fromkeys(_public_failure_message(result) for result in failures))
    details = "\n".join(f"- {message}" for message in messages)
    return f"## 本次执行未完成\n\n{details}"


def _public_failure_message(result: TaskResultModel) -> str:
    try:
        payload = json.loads(result.content)
    except (TypeError, json.JSONDecodeError):
        return result.content
    if not isinstance(payload, dict):
        return result.content
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return "所需证据暂时无法获取。"


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
