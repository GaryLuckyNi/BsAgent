"""验证真实路由、材料传递和受控修订；模型响应在提供方边界替代。"""

import json

import pytest
from app.graph.state import create_initial_state
from app.intent.schemas import IntentDecisionModel
from app.llm.schemas import LlmCompletionResult
from langchain_core.messages import HumanMessage
from pydantic import ValidationError


def intent(**changes):
    return {
        "intent_type": "financial_research", "domain": "a_share_equity_research",
        "subdomain": "company_snapshot", "goal": "分析已有材料", "confidence": 0.9,
        **changes,
    }


def state():
    value = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="分析已有材料",
    )
    value["request_flow_version"] = "context-routing-v1"
    value["intent"] = intent(available_information={"用户数据": {"收入": 12, "期间": "2025"}})
    return value


class Answers:
    def __init__(self, *answers):
        self.answers = answers
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        return LlmCompletionResult(
            content=answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False),
            model="test", provider="test",
        )


def request():
    return {
        "reason_type": "evidence_conflict", "affected_goal": "分析已有材料",
        "evidence_refs": ["task:facts"],
        "existing_plan_limitation": "原计划未安排财报重述核验，无法解释同一期收入冲突",
        "required_changes": ["核实财报重述公告"],
    }


def test_intent_keeps_free_json_and_enforces_response_branch():
    information = {"任何字段": [1, None, {"用户提供": True}]}
    decision = IntentDecisionModel.model_validate(intent(available_information=information))
    assert decision.available_information == information
    assert decision.route == "plan"
    for extra in (
        {"route": "direct_answer"},
        {"route": "plan", "answer": "提前回答"},
        {"available_information": {"非法": object()}},
        {"available_information": {"非法": float("nan")}},
    ):
        with pytest.raises(ValidationError):
            IntentDecisionModel.model_validate(intent(**extra))


def test_context_has_stable_refs_without_promoting_assistant_text():
    from app.graph.planning.context import request_context

    value = state()
    value["messages"].insert(0, HumanMessage(id="prior", content="上年收入12亿元"))
    payload = request_context(value)
    assert payload["available_information"] == value["intent"]["available_information"]
    assert any(item["ref"] == "message:prior" for item in payload["materials"])
    assert value["tool_calls"] == [] and value["task_results"] == []


def test_router_answers_in_one_call_and_preserves_information(business_intent_catalog):
    from app.graph.nodes.intent_router import build_intent_router_node
    from app.graph.runtime import _route_after_intent

    llm = Answers(intent(route="direct_answer", answer="市盈率为股价与每股收益之比。",
                         available_information={"已有材料": ["用户问题"]}))
    value = state()
    build_intent_router_node(catalog=business_intent_catalog, llm_client=llm)(value)
    assert len(llm.calls) == 1
    assert _route_after_intent(value) == "verifier"
    assert "市盈率" in value["draft_answer"]["content"]
    assert value["intent"]["available_information"] == {"已有材料": ["用户问题"]}


def test_router_can_send_existing_evidence_directly_to_synthesis():
    from app.graph.runtime import _route_after_intent

    value = state()
    value["intent"]["route"] = "synthesize"
    assert _route_after_intent(value) == "synthesizer"


def test_planner_receives_available_information_in_existing_call():
    from app.graph.nodes.planner_agent import build_planner_agent_node

    llm = Answers({"tasks": [{"id": "news", "task_goal": "核实近期公告"}]})
    value = state()
    build_planner_agent_node(llm_client=llm)(value)
    payload = json.loads(llm.calls[0]["user_prompt"])
    assert payload["planning_constraints"]["request_context"]["available_information"] == (
        value["intent"]["available_information"]
    )
    assert len(llm.calls) == 1


def test_new_scheduler_advances_dependencies_without_review():
    from app.graph.runtime import _dispatch_after_scheduler

    value = state()
    value["plan"] = {"tasks": [
        {"id": "facts", "task_goal": "查询事实"},
        {"id": "use_facts", "task_goal": "根据事实查询公告", "depends_on": ["facts"]},
    ]}
    value["task_results"] = [{"task_id": "facts", "status": "success", "content": "事实"}]
    dispatch = _dispatch_after_scheduler(value)
    assert isinstance(dispatch, list)
    assert [item.arg["active_task"]["id"] for item in dispatch] == ["use_facts"]
    value["task_results"].append({"task_id": "use_facts", "status": "success", "content": "公告"})
    assert _dispatch_after_scheduler(value) == "synthesizer"


def test_synthesis_decision_is_exclusive():
    from app.graph.planning.synthesis_decision import SynthesisDecision

    decision = SynthesisDecision.model_validate({"action": "request_replan", "replan_request": request()})
    assert decision.answer is None
    for payload in (
        {"action": "request_replan"},
        {"action": "answer", "answer": "结论", "replan_request": request()},
        {"action": "answer", "answer": "  "},
    ):
        with pytest.raises(ValidationError):
            SynthesisDecision.model_validate(payload)


def test_synthesis_answers_and_assesses_in_one_call():
    from app.graph.nodes.synthesizer import build_synthesizer_node

    llm = Answers({"action": "answer", "answer": "用户提供的收入为12，尚缺现金流信息。"})
    value = state()
    result = build_synthesizer_node(llm_client=llm)(value)
    assert "用户提供的收入为12" in result["draft_answer"]["content"]
    assert '"action"' not in result["draft_answer"]["content"]
    assert len(llm.calls) == 1
    payload = json.loads(llm.calls[0]["user_prompt"])
    assert payload["request_context"]["available_information"] == value["intent"]["available_information"]
    assert payload["allow_replan"] is False


def test_synthesis_requests_revision_without_publishing_answer():
    from app.graph.nodes.synthesizer import build_synthesizer_node
    from app.graph.runtime import _route_after_synthesis

    value = state()
    value["planner_iteration"] = 1
    value["plan"] = {"tasks": [{"id": "facts", "task_goal": "查询财报"}]}
    value["task_results"] = [{"task_id": "facts", "status": "success", "content": "收入数字冲突"}]
    llm = Answers({"action": "request_replan", "replan_request": request()})
    build_synthesizer_node(llm_client=llm)(value)
    assert value["draft_answer"] is None
    assert _route_after_synthesis(value) == "planner_agent"
    assert value["pending_replan"]["request_id"] == value["request_id"]
    assert value["replan_request_count"] == 1
    assert len(llm.calls) == 1


def test_rejected_answer_is_not_published():
    from app.graph.nodes.verifier import verifier_node
    from app.graph.runtime import final_response_node

    value = state()
    value["draft_answer"] = {"content": "保证收益，建议买入。", "response_type": "research_answer"}
    verifier_node(value)
    final_response_node(value)
    assert "保证收益" not in value["final_response"]["content"]


def test_planner_does_not_review_without_a_bound_replan_request():
    from app.graph.nodes.planner_agent import build_planner_agent_node

    value = state()
    value["plan"] = {"tasks": [{"id": "facts", "task_goal": "查询财报"}]}
    llm = Answers({"action": "finish", "reason": "不应进行的例行评审"})
    build_planner_agent_node(llm_client=llm)(value)
    assert llm.calls == []
    assert value["replan_stop_reason"] == "replan_request_unbound"


def test_revision_is_consumed_once_and_only_new_work_is_namespaced():
    from app.graph.nodes.planner_agent import build_planner_agent_node
    from app.graph.planning.request_flow import accept_replan

    value = state()
    value["planner_iteration"] = 1
    value["plan"] = {"tasks": [{"id": "facts", "task_goal": "查询财报"}]}
    value["task_results"] = [{"task_id": "facts", "status": "success", "content": "收入冲突"}]
    assert accept_replan(value, request()) == "planner_agent"
    llm = Answers({"action": "revise", "reason": "补充重述公告核验", "plan": {"tasks": [
        {"id": "verify", "task_goal": "核实2025年收入重述公告", "evidence_task_ids": ["facts"]},
    ]}})
    build_planner_agent_node(llm_client=llm)(value)
    assert value["pending_replan"] is None
    assert value["synthesis_decision"] is None
    assert value["plan"]["tasks"][0]["id"] == "r1_verify"
    assert value["task_results"][0]["task_id"] == "facts"
    prompt = json.loads(llm.calls[0]["user_prompt"])
    assert prompt["replan_request"]["reason_type"] == "evidence_conflict"
    assert "continue" not in prompt["required_schema"]
    assert len(llm.calls) == 1


def test_worker_contexts_can_use_available_information():
    from app.graph.nodes.analysis_agent import build_analysis_agent_node
    from app.graph.nodes.tool_agent import _tool_call_messages
    from app.graph.planning.contracts import SubTaskModel

    value = state()
    value["active_task"] = {"id": "calc", "task_goal": "根据用户材料计算", "execution_type": "analysis"}
    llm = Answers("用户给出的收入为12。")
    build_analysis_agent_node(llm_client=llm)(value)
    analysis = json.loads(llm.calls[0]["user_prompt"])
    tool = json.loads(_tool_call_messages(value, SubTaskModel.model_validate(value["active_task"]))[-1].content)
    for payload in (analysis, tool):
        assert payload["request_context"]["available_information"] == value["intent"]["available_information"]
        assert "user_materials" not in payload


def test_new_scheduler_trace_does_not_claim_every_result_needs_review():
    from app.operations.langfuse_scheduler import scheduler_trace_input

    payload = scheduler_trace_input(state())
    assert payload["request_flow"]["version"] == "context-routing-v1"
    assert "planner_feedback_remaining" not in payload
    assert "reviewed" not in payload


def test_synthesis_prompt_prioritizes_required_verification_over_a_limitations_only_answer():
    from app.graph.nodes.synthesizer import build_synthesizer_node

    value = state()
    value.update(planner_iteration=1, plan={"tasks": [{"id": "facts", "task_goal": "查收入"}]},
                 task_results=[{"task_id": "facts", "status": "success", "content": "同口径收入13亿与16亿冲突"}])
    llm = Answers({"action": "answer", "answer": "有限结论"})
    build_synthesizer_node(llm_client=llm)(value)
    prompt = llm.calls[0]["system_prompt"]
    assert "不能仅列核验建议后结束" in prompt
    assert "先判断必须完成的目标" in prompt


@pytest.mark.parametrize("change,expected", [
    ("unknown_ref", "replan_evidence_ref_invalid"),
    ("budget", "replan_budget_exhausted"),
    ("pending_task", "replan_pending_execution"),
])
def test_revision_rejects_unknown_evidence_budget_and_unfinished_work(change, expected):
    from app.graph.planning.request_flow import accept_replan

    value = state()
    value.update(planner_iteration=1, plan={"tasks": [{"id": "facts", "task_goal": "查收入"}]},
                 task_results=[{"task_id": "facts", "status": "success", "content": "冲突"}])
    proposal = request()
    if change == "unknown_ref":
        proposal["evidence_refs"] = ["task:invented"]
    elif change == "budget":
        value["replan_request_count"] = 1
    else:
        value["task_results"] = []
    assert accept_replan(value, proposal) == "synthesizer"
    assert value["pending_replan"] is None
    assert value["replan_stop_reason"] == expected


def test_malformed_synthesis_is_repaired_at_most_once_and_not_published():
    from app.graph.nodes.synthesizer import build_synthesizer_node

    value = state()
    llm = Answers('{"action":"request_replan"}')
    build_synthesizer_node(llm_client=llm)(value)
    assert len(llm.calls) == 2
    assert value["replan_stop_reason"] == "synthesis_decision_invalid"
    assert '"action"' not in value["draft_answer"]["content"]


@pytest.mark.parametrize("reason", [
    "skill_workflow_invalid", "provider_permission_denied", "provider_permission_fallback_complete",
])
def test_local_failure_does_not_stop_independent_new_flow_tasks(reason):
    from app.graph.runtime import _dispatch_after_scheduler

    value = state()
    value.update(planner_stop_reason=reason, plan={"tasks": [
        {"id": "facts", "task_goal": "查询财报"}, {"id": "independent", "task_goal": "查询独立公告"},
    ]}, task_results=[{"task_id": "facts", "status": "terminal_failure", "content": "失败"}])
    dispatch = _dispatch_after_scheduler(value)
    assert isinstance(dispatch, list)
    assert [row.arg["active_task"]["id"] for row in dispatch] == ["independent"]


@pytest.mark.parametrize("borrow_existing_id", [False, True])
def test_revision_cannot_replay_failed_task_by_changing_only_id(borrow_existing_id):
    from app.graph.nodes.planner_agent import build_planner_agent_node
    from app.graph.planning.request_flow import accept_replan

    value = state()
    value.update(planner_iteration=1, plan={"tasks": [{"id": "facts", "task_goal": "查询财报"}]},
                 task_results=[{"task_id": "facts", "status": "terminal_failure",
                                "error_code": "invalid_tool_arguments", "content": "参数错误"}])
    if borrow_existing_id:
        value["plan"]["tasks"].append({
            "id": "use_facts", "task_goal": "依据财报查询公告", "depends_on": ["facts"],
        })
    accept_replan(value, request())
    llm = Answers({"action": "revise", "reason": "只改ID重试", "plan": {"tasks": [
        {"id": "use_facts" if borrow_existing_id else "retry_facts", "task_goal": "查询财报"},
    ]}})
    build_planner_agent_node(llm_client=llm)(value)
    assert value["planner_iteration"] == 1
    assert value["pending_replan"] is None
    assert value["replan_stop_reason"] == "replan_output_invalid"


def test_synthesis_keeps_removed_failed_tasks_as_execution_limits_not_evidence():
    from app.graph.nodes.synthesizer import build_synthesizer_node

    value = state()
    value.update(
        planner_iteration=2, replan_request_count=1,
        plan={"tasks": [{"id": "verify", "task_goal": "核验收入"}]},
        plan_history=[{"revision": 0, "plan": {"tasks": [
            {"id": "cashflow", "task_goal": "核实经营现金流"},
        ]}}],
        task_results=[
            {"task_id": "cashflow", "status": "terminal_failure", "error_code": "provider_permission_denied", "content": "未取得现金流"},
            {"task_id": "verify", "status": "success", "content": "收入已核实"},
        ],
    )
    llm = Answers({"action": "answer", "answer": "收入已核实，现金流仍未核实。"})
    build_synthesizer_node(llm_client=llm)(value)
    payload = json.loads(llm.calls[0]["user_prompt"])
    assert payload["historical_execution_limits"][0]["task_goal"] == "核实经营现金流"
    assert "未取得现金流" not in json.dumps(payload["research_materials"], ensure_ascii=False)


def test_planner_leaves_final_arithmetic_to_synthesis():
    from app.graph.nodes.planner_agent import build_planner_agent_node

    llm = Answers({"tasks": [{"id": "price", "task_goal": "查询最近交易日股价"}]})
    build_planner_agent_node(llm_client=llm)(state())
    assert "最终简单算术由合成层完成" in llm.calls[0]["system_prompt"]
