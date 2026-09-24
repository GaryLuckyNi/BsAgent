import json

import pytest
from pydantic import ValidationError

from app.graph.planning.contracts import PlanModel, TaskResultModel
from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities, PlannerOutputError
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult


class Decisions:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        return LlmCompletionResult(content=json.dumps(payload), model="test", provider="test")


class InvalidFeedback:
    def __init__(self):
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(content="无法给出有效 JSON", model="test", provider="test")


def plan():
    return PlanModel(
        tasks=[
            {"id": "facts", "task_goal": "核实事实"},
            {
                "id": "analysis",
                "task_goal": "分析",
                "depends_on": ["facts"],
                "execution_type": "analysis",
            },
        ]
    )


def review(client, *, results=None, current=None, remaining=1):
    return LlmTaskPlanner(llm_client=client).review(
        goal="分析原因",
        user_message="只使用提供的资料分析原因",
        current_plan=current or plan(),
        task_results=tuple(
            results or [TaskResultModel(task_id="facts", status="success", content="已取得事实")]
        ),
        capabilities=PlannerCapabilities(),
        remaining_revisions=remaining,
    )


@pytest.mark.parametrize("action", ["continue", "finish", "revise"])
def test_model_chooses_action_for_same_observation(action):
    payload = {"action": action, "reason": "根据证据选择"}
    if action == "revise":
        payload["plan"] = {"tasks": [{"id": "more", "task_goal": "补充证据"}]}
    decision = review(Decisions(payload))
    assert decision.action == action


def test_no_progress_continue_is_returned_to_model_for_repair():
    client = Decisions(
        {"action": "continue", "reason": "继续"},
        {"action": "finish", "reason": "没有可推进任务，说明限制"},
    )
    result = review(
        client,
        results=[TaskResultModel(task_id="facts", status="business_failure", content="无权限")],
    )
    assert result.action == "finish"
    assert len(client.calls) == 2
    assert "可执行" in client.calls[1]["user_prompt"]


def test_revision_cannot_rewrite_completed_task():
    client = Decisions(
        {
            "action": "revise",
            "reason": "修改",
            "plan": {"tasks": [{"id": "facts", "task_goal": "更换目标"}]},
        }
    )
    with pytest.raises(PlannerOutputError):
        review(client)


def test_revision_budget_does_not_forbid_finish():
    assert review(Decisions({"action": "finish", "reason": "完成"}), remaining=0).action == "finish"
    with pytest.raises(PlannerOutputError):
        review(
            Decisions(
                {
                    "action": "revise",
                    "reason": "补查",
                    "plan": {"tasks": [{"id": "more", "task_goal": "补查"}]},
                }
            ),
            remaining=0,
        )


def test_feedback_context_includes_original_goal_plan_and_observations():
    client = Decisions({"action": "finish", "reason": "证据足够"})
    review(client)
    data = json.loads(client.calls[0]["user_prompt"])
    assert data["user_message"] == "只使用提供的资料分析原因"
    assert data["current_plan"]["tasks"][0]["id"] == "facts"
    assert data["observations"][0]["content"] == "已取得事实"
    assert data["required_schema"]["continue"]["action"] == "continue"
    assert "$defs" not in data["required_schema"]


def test_analysis_task_cannot_also_bind_skill():
    with pytest.raises(ValidationError):
        PlanModel(
            tasks=[
                {
                    "id": "bad",
                    "task_goal": "分析",
                    "execution_type": "analysis",
                    "skill_name": "skill",
                }
            ]
        )


def test_revision_keeps_completed_evidence_and_namespaces_only_new_tasks():
    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="分析原因"
    )
    state.update(
        intent={
            "intent_type": "research",
            "domain": "equity",
            "subdomain": "company",
            "goal": "分析原因",
        },
        plan=plan().model_dump(),
        planner_iteration=1,
    )
    state["task_results"] = [{"task_id": "facts", "status": "success", "content": "已取得事实"}]
    revised = {
        "tasks": [
            plan().tasks[0].model_dump(),
            {
                "id": "verify",
                "task_goal": "进一步核实",
                "depends_on": ["facts"],
                "tool_search_query": "公告",
            },
        ]
    }
    result = build_planner_agent_node(
        llm_client=Decisions({"action": "revise", "reason": "缺少核实", "plan": revised})
    )(state)
    assert [t["id"] for t in result["plan"]["tasks"]] == ["facts", "r1_verify"]
    assert result["plan"]["tasks"][1]["depends_on"] == ["facts"]
    assert result["plan"]["tasks"][1]["tool_search_query"] == "公告"
    assert result["plan_history"][0]["plan"]["tasks"][1]["id"] == "analysis"
    assert result["planner_feedback_count"] == 1


def test_scheduler_requests_feedback_for_success_and_failure():
    from app.graph.runtime import _dispatch_after_scheduler

    for status in ("success", "business_failure", "terminal_failure"):
        state = create_initial_state(
            session_id="s", tenant_id="t", user_id="u", user_message="研究"
        )
        state.update(plan=plan().model_dump(), planner_iteration=1)
        state["task_results"] = [{"task_id": "facts", "status": status, "content": "观察"}]
        assert _dispatch_after_scheduler(state) == "planner_agent"


def test_scheduler_obeys_reviewed_continue_and_finish():
    from app.graph.runtime import _dispatch_after_scheduler
    from app.graph.planning.policy import observation_signature

    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="研究")
    state.update(plan=plan().model_dump(), planner_iteration=1)
    state["task_results"] = [{"task_id": "facts", "status": "success", "content": "观察"}]
    state["planner_reviewed_signature"] = observation_signature(plan(), state["task_results"])
    state["planner_decision"] = {"action": "continue", "reason": "继续分析"}
    sends = _dispatch_after_scheduler(state)
    assert sends[0].node == "analysis_agent"
    state["planner_decision"] = {"action": "finish", "reason": "证据足够"}
    assert _dispatch_after_scheduler(state) == "synthesizer"


def test_feedback_budget_stop_is_runtime_limit_not_model_decision():
    from app.graph.runtime import task_scheduler_node
    from app.graph.planning.policy import MAX_PLANNER_FEEDBACKS

    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="研究")
    state.update(
        plan=plan().model_dump(), planner_iteration=1, planner_feedback_count=MAX_PLANNER_FEEDBACKS
    )
    state["task_results"] = [{"task_id": "facts", "status": "success", "content": "事实"}]
    update = task_scheduler_node(state)
    assert update["planner_stop_reason"] == "planner_feedback_budget_exhausted"
    assert "planner_decision" not in update


def test_invalid_feedback_stops_safely_without_claiming_a_model_decision():
    from app.graph.runtime import _dispatch_after_scheduler

    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="分析原因"
    )
    state.update(
        intent={
            "intent_type": "research",
            "domain": "equity",
            "subdomain": "company",
            "goal": "分析原因",
        },
        plan=plan().model_dump(),
        planner_iteration=1,
    )
    state["task_results"] = [{"task_id": "facts", "status": "success", "content": "已取得部分事实"}]
    client = InvalidFeedback()
    result = build_planner_agent_node(llm_client=client)(state)
    assert len(client.calls) == 2
    assert result["planner_stop_reason"] == "planner_feedback_invalid"
    assert not result.get("planner_decision")
    assert _dispatch_after_scheduler(result) == "synthesizer"


def test_resume_policy_rejects_old_checkpoint():
    from app.graph.runtime import _validate_planner_checkpoint, LegacyCheckpointIncompatibleError

    with pytest.raises(LegacyCheckpointIncompatibleError):
        _validate_planner_checkpoint({"plan": plan().model_dump()})
    _validate_planner_checkpoint({"planner_policy_version": "model-feedback-v1"})


def test_trace_records_feedback_action_without_private_reason():
    from app.harness.tracing import summarize_node_transition

    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="研究")
    state.update(
        plan=plan().model_dump(),
        planner_iteration=1,
        planner_feedback_count=1,
        planner_decision={"action": "finish", "reason": "私有业务细节"},
    )
    summary = summarize_node_transition(
        node_name="planner_agent", input_state=state, output_state=state
    )
    assert summary["planner_decision"]["action"] == "finish"
    assert summary["planner_decision"]["policy_version"] == "model-feedback-v1"
    assert "私有业务细节" not in json.dumps(summary, ensure_ascii=False)


def test_revision_can_reference_archived_success_without_reexecuting_it():
    client = Decisions(
        {
            "action": "revise",
            "reason": "使用旧证据",
            "plan": {
                "tasks": [
                    {
                        "id": "new_analysis",
                        "task_goal": "比较",
                        "execution_type": "analysis",
                        "evidence_task_ids": ["archived"],
                    }
                ]
            },
        }
    )
    result = review(
        client, results=[TaskResultModel(task_id="archived", status="success", content="旧事实")]
    )
    assert result.plan.tasks[0].evidence_task_ids == ["archived"]


def test_revision_rejects_unavailable_evidence_reference():
    client = Decisions(
        {
            "action": "revise",
            "reason": "引用不存在证据",
            "plan": {
                "tasks": [
                    {"id": "new_analysis", "task_goal": "比较", "evidence_task_ids": ["missing"]}
                ]
            },
        }
    )
    with pytest.raises(PlannerOutputError):
        review(client)


def test_synthesis_receives_model_assessment_and_unresolved_questions():
    from app.graph.nodes.synthesizer import build_synthesizer_node

    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="分析全年变化"
    )
    state.update(
        intent={"intent_type": "research", "goal": "分析全年变化"},
        plan=plan().model_dump(),
        planner_decision={
            "action": "finish",
            "reason": "只能确定单季度情况",
            "unresolved_questions": ["尚无全年可比数据"],
        },
    )
    state["task_results"] = [
        {"task_id": t.id, "status": "success", "content": "单季数据"} for t in plan().tasks
    ]
    client = Decisions({"answer": "材料不足"})
    build_synthesizer_node(llm_client=client)(state)
    payload = json.loads(client.calls[0]["user_prompt"])
    assert "尚无全年可比数据" in payload["data_limitations"]
    assert payload["research_assessment"]["reason"] == "只能确定单季度情况"
    assert state["draft_answer"]["confidence"] < 1.0


def test_revision_namespace_avoids_completed_id_collisions():
    from app.graph.nodes.planner_agent import _namespace_replanned_tasks

    original = PlanModel(
        tasks=[
            {"id": "r1_x", "task_goal": "旧任务"},
            {"id": "x", "task_goal": "新任务", "depends_on": ["r1_x"]},
        ]
    )
    revised = _namespace_replanned_tasks(original, revision=1, preserved_ids={"r1_x"})
    assert revised.tasks[0].id == "r1_x"
    assert revised.tasks[1].id != "r1_x"
    assert revised.tasks[1].depends_on == ["r1_x"]
