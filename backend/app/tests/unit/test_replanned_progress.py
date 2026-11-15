"""重规划替换的是公开活动状态，不篡改原任务执行事实。"""

import pytest
from app.graph import public_events


def failed_state():
    task = {"id": "skill", "task_goal": "使用 report Skill 获取报告", "skill_name": "report"}
    return {
        "active_task": task, "plan": {"tasks": [task]}, "planner_iteration": 1,
        "task_results": [{"task_id": "skill", "status": "terminal_failure", "content": "权限拒绝"}],
    }


def test_failed_skill_is_pending_until_final_answer(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    state = failed_state()
    public_events.emit_public_progress("skill_agent", state, status="completed")
    assert emitted[-1]["status"] == "pending"
    assert "未完成" not in emitted[-1]["message"]
    state["draft_answer"] = {"content": "现有材料无法完成查询。"}
    public_events.emit_public_progress("synthesizer", state, status="completed")
    failure = next(event for event in emitted if event["status"] == "failed")
    assert failure["id"] == "skill_agent:skill"
    assert "研究流程未完成" in failure["message"]


def test_accepted_replan_replaces_failure_in_place_without_claiming_success(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    state = failed_state()
    previous_plan = state["plan"]
    state["plan"] = {"tasks": [{"id": "r1_search", "task_goal": "搜索公开披露"}]}
    state["planner_iteration"] = 2
    public_events.emit_replanned_task_progress(state, previous_plan=previous_plan, previous_iteration=1)
    assert emitted[0]["id"] == "skill_agent:skill"
    assert emitted[0]["status"] == "superseded"
    assert "已调整研究流程" in emitted[0]["message"]
    assert "已完成" not in emitted[0]["message"]
    assert state["task_results"][0]["status"] == "terminal_failure"
    state["draft_answer"] = {"content": "根据补充资料作答。"}
    public_events.emit_public_progress("synthesizer", state, status="completed")
    assert not any(event["status"] == "failed" for event in emitted)


@pytest.mark.parametrize("draft,pending", [(None, {"request": {}}), (None, None)])
def test_synthesis_requesting_replan_does_not_finalize_failure(monkeypatch, draft, pending):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    state = {**failed_state(), "draft_answer": draft, "pending_replan": pending}
    public_events.emit_public_progress("synthesizer", state, status="completed")
    assert not any(event["status"] == "failed" for event in emitted)


def test_failed_compensation_warns_for_new_path_without_reviving_old_failure(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    state = failed_state()
    previous = state["plan"]
    state["plan"] = {"tasks": [{"id": "replacement", "task_goal": "替代来源查询"}]}
    public_events.emit_replanned_task_progress(state, previous_plan=previous, previous_iteration=1)
    state["task_results"].append({"task_id": "replacement", "status": "business_failure"})
    state["draft_answer"] = {"content": "已无法取得所需证据。"}
    state["replan_stop_reason"] = "replan_budget_exhausted"
    public_events.emit_public_progress("synthesizer", state, status="completed")
    assert [(event["id"], event["status"]) for event in emitted if event["kind"] == "activity"] == [
        ("skill_agent:skill", "superseded"), ("tool_agent:replacement", "failed"),
    ]


def test_skill_presentation_rule_is_shared_by_initial_and_feedback_prompts():
    from app.graph.planning.planner import _FEEDBACK_PROMPT, _SYSTEM_PROMPT

    for prompt in (_SYSTEM_PROMPT, _FEEDBACK_PROMPT):
        assert "Skill 任务的 task_goal 只写" in prompt
        assert "不复制 Skill 正文" in prompt


def test_replan_also_retires_failed_skill_step_activity(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    state = failed_state()
    previous_plan = state["plan"]
    state["skill_workflows"] = {"skill": {"steps": [
        {"task_id": "skill_step_1", "instruction": "获取业务数据", "tool_name": "mcp.source.query"},
    ]}}
    state["task_results"].append({"task_id": "skill_step_1", "status": "failed"})
    state["plan"] = {"tasks": [{"id": "replacement", "task_goal": "查询其他来源"}]}
    public_events.emit_replanned_task_progress(state, previous_plan=previous_plan, previous_iteration=1)
    assert {event["id"] for event in emitted} == {"skill_agent:skill", "tool_agent:skill_step_1"}
    assert all(event["status"] == "superseded" for event in emitted)


def test_planner_node_publishes_replacement_only_after_valid_revision(monkeypatch):
    from app.graph.nodes.planner_agent import build_planner_agent_node
    from app.graph.planning.contracts import PlanModel, PlannerFeedback
    from app.graph.planning.planner import LlmTaskPlanner
    from app.graph.state import create_initial_state

    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    monkeypatch.setattr(LlmTaskPlanner, "review", lambda *_args, **_kwargs: PlannerFeedback(
        action="revise", reason="替代不可用路径", plan=PlanModel(tasks=[
            {"id": "search", "task_goal": "搜索公开披露"},
        ]),
    ))
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="查询报告")
    state.update(failed_state())
    state["intent"] = {"intent_type": "research", "domain": "finance", "subdomain": "company", "goal": "查询报告"}
    result = build_planner_agent_node(llm_client=object())(state)
    assert result["plan"]["tasks"][0]["id"] == "r1_search"
    assert emitted[-1]["id"] == "skill_agent:skill"
    assert emitted[-1]["status"] == "superseded"


def test_saved_progress_keeps_recovery_states_and_original_position():
    from app.api.sessions import _collect_research_process_event

    events = []
    for status in ("pending", "superseded"):
        _collect_research_process_event(events, {
            "id": "skill_agent:skill", "kind": "activity", "label": "研究流程",
            "message": "待调整" if status == "pending" else "已调整研究流程", "status": status,
        })
        assert len(events) == 1
        assert events[0]["status"] == status
        assert events[0]["sequence"] == 0


def test_planner_attempt_failure_does_not_finalize_before_outer_retry(monkeypatch):
    from app.graph.runtime import AgentGraphExecutionError, _with_harness

    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    state = {**failed_state(), "errors": []}
    public_events.emit_public_progress("skill_agent", state, status="completed")

    class Runner:
        def run(self, _name, node, state, **_kwargs):
            return node(state)

    def broken_planner(_):
        raise RuntimeError("planner unavailable")

    with pytest.raises(AgentGraphExecutionError):
        _with_harness("planner_agent", broken_planner, Runner())(state)
    updates = [event for event in emitted if event["id"] == "skill_agent:skill"]
    assert [event["status"] for event in updates] == ["pending"]


def test_real_graph_retry_can_replan_without_transient_failure_warning(monkeypatch):
    from app.graph.runtime import _OPERATIONAL_RETRY_POLICY, _with_harness
    from app.graph.state import AgentState, create_initial_state
    from langgraph.graph import END, StateGraph

    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="研究")
    state.update(failed_state())
    public_events.emit_public_progress("skill_agent", state, status="completed")
    attempts = []

    class Runner:
        def run(self, _name, node, state, **_kwargs):
            return node(state)

    def planner(state):
        attempts.append(True)
        if len(attempts) == 1:
            error = RuntimeError("model timeout")
            error.code = "llm_timeout"
            raise error
        previous = state["plan"]
        updated = {"plan": {"tasks": [{"id": "replacement", "task_goal": "补充取证"}]}, "planner_iteration": 2}
        public_events.emit_replanned_task_progress({**state, **updated}, previous_plan=previous, previous_iteration=1)
        return updated

    builder = StateGraph(AgentState)
    builder.add_node("planner_agent", _with_harness("planner_agent", planner, Runner()),
                     retry_policy=_OPERATIONAL_RETRY_POLICY._replace(initial_interval=0))
    builder.set_entry_point("planner_agent")
    builder.add_edge("planner_agent", END)
    builder.compile().invoke(state)
    assert len(attempts) == 2
    assert [event["status"] for event in emitted if event["id"] == "skill_agent:skill"] == ["pending", "superseded"]
