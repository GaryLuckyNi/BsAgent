"""真实 LangGraph + Langfuse SDK 的调度观测，不调用外部模型或工具。"""

import copy
import json
from uuid import uuid4

import httpx
import pytest
from app.core.config import Settings
from app.graph.planning.contracts import PlanModel
from app.graph.planning.policy import MAX_PLANNER_FEEDBACKS, observation_signature
from app.graph.runtime import _dispatch_after_scheduler, task_scheduler_node
from app.graph.state import AgentState, create_initial_state
from app.operations import langfuse_observability
from app.operations.langfuse_observability import configure_langfuse, langfuse_agent_run
from app.operations.langfuse_scheduler import scheduler_trace_input
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from langfuse import Langfuse

INPUT = "langfuse.observation.input"
OUTPUT = "langfuse.observation.output"


def _state():
    state = create_initial_state(
        session_id="private-session", tenant_id="private-tenant", user_id="private-user",
        user_message="查行情，Authorization: Bearer synthetic-credential",
    )
    state["plan"] = {
        "tasks": [{"id": "market", "task_goal": "查询行情", "execution_type": "tool"}],
    }
    return state


def test_scheduler_trace_preserves_explicit_synthesis_handoff():
    state = _state()
    state["planner_decision"] = {
        "action": "finish", "reason": "取证完成，由回答节点承接最终分析",
        "synthesis_task_id": "final_analysis", "unresolved_questions": ["无需重复写入调度观测"],
    }
    assert scheduler_trace_input(state)["planner_decision"] == {
        "action": "finish", "reason": "取证完成，由回答节点承接最终分析",
        "synthesis_task_id": "final_analysis",
    }


@pytest.fixture
def telemetry(request):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    params = getattr(getattr(request.node, "callspec", None), "params", {})
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))) as http:
        config = Settings(
            _env_file=None, langfuse_enabled=True,
            langfuse_public_key=f"pk-lf-test-{uuid4().hex}",
            langfuse_secret_key="sk-lf-test-synthetic",
            langfuse_content_mode=params.get("mode", "local_content"),
            langfuse_max_content_chars=params.get("limit", 20000),
        )
        runtime = configure_langfuse(
            config=config, provider_factory=lambda **_: provider,
            client_factory=lambda **kwargs: Langfuse(
                **kwargs, span_exporter=exporter, httpx_client=http,
            ),
        )
        try:
            yield runtime, exporter
        finally:
            runtime.shutdown()


@pytest.mark.parametrize("mode", ["local_content", "metadata_only"])
def test_real_graph_exports_scheduler_update_and_dispatch_reason(telemetry, mode):
    runtime, exporter = telemetry
    graph = StateGraph(AgentState)
    graph.add_node("task_scheduler", task_scheduler_node)

    def tool(state):
        return {"task_results": [{
            "task_id": "market", "status": "success", "content": "private-tool-result",
        }]}

    def planner(state):
        return {
            "planner_reviewed_signature": observation_signature(
                PlanModel.model_validate(state["plan"]), state["task_results"],
            ),
            "planner_decision": {"action": "finish", "reason": "证据足够"},
            "planner_feedback_count": 1,
        }

    graph.add_node("tool_agent", tool)
    graph.add_node("planner_agent", planner)
    graph.add_node("synthesizer", lambda _: {})
    graph.add_edge(START, "task_scheduler")
    graph.add_conditional_edges("task_scheduler", _dispatch_after_scheduler)
    graph.add_edge("tool_agent", "task_scheduler")
    graph.add_edge("planner_agent", "task_scheduler")
    graph.add_edge("synthesizer", END)
    with langfuse_agent_run(
        business_trace_id=f"test-scheduler-{uuid4().hex}", session_key="s", tenant_id="t",
        execution_segment="initial",
    ) as run:
        result = graph.compile().invoke(_state(), config={"callbacks": list(run)})
    runtime.client.flush()
    runtime.provider.force_flush()
    spans = exporter.get_finished_spans()
    dispatches = [span for span in spans if span.name == "dispatch_task_scheduler"]
    assert len(dispatches) == 3
    assert result["planner_feedback_count"] == 1
    assert not any(span.attributes.get("langfuse.observation.type") == "generation" for span in spans)
    schedulers = [span for span in spans if span.name == "task_scheduler"]
    assert len(schedulers) == 3
    for span in [*dispatches, *schedulers]:
        if mode == "metadata_only":
            assert INPUT not in span.attributes
            assert OUTPUT not in span.attributes
        else:
            assert INPUT in span.attributes, span.name
            assert OUTPUT in span.attributes, span.name
            serialized = json.dumps(dict(span.attributes))
            for private in ("private-tenant", "private-session", "private-user",
                            "private-tool-result", "synthetic-credential"):
                assert private not in serialized
    if mode == "local_content":
        by_reason = {json.loads(span.attributes[OUTPUT])["reason"]: span for span in dispatches}
        assert set(by_reason) == {
            "ready_tasks", "new_results_require_review", "planner_finished",
        }
        initial = json.loads(by_reason["ready_tasks"].attributes[OUTPUT])
        assert initial["next_node"] == "tool_agent"
        assert initial["dispatched_tasks"][0]["task_id"] == "market"
        review = by_reason["new_results_require_review"]
        assert json.loads(review.attributes[OUTPUT])["next_node"] == "planner_agent"
        review_input = json.loads(review.attributes[INPUT])
        assert review_input["has_observations"] is True
        assert review_input["reviewed"] is False
        assert review_input["task_results"][0]["status"] == "success"
        assert all(json.loads(span.attributes[OUTPUT]) == {} for span in schedulers)
    # 手工 span 必须处在实际分发回调内，不能漂到独立 trace。
    parents = {span.context.span_id for span in spans if span.name == "_dispatch_after_scheduler"}
    assert all(span.parent.span_id in parents for span in dispatches)


@pytest.mark.parametrize(
    "case,target,reason",
    [
        ("stop", "synthesizer", "planner_stopped"),
        ("budget", "synthesizer", "planner_feedback_budget_exhausted"),
        ("approval", "approval_gate", "approval_required"),
        ("workflow_failed", "synthesizer", "skill_workflow_failed"),
        ("no_ready", "synthesizer", "no_ready_tasks"),
    ],
)
def test_dispatch_reasons_match_actual_branch_without_mutating_state(telemetry, case, target, reason):
    from app.schemas.approvals import PendingToolApproval

    runtime, exporter = telemetry
    state = _state()
    if case == "stop":
        state["planner_stop_reason"] = "provider_permission_denied"
    elif case in {"budget", "no_ready"}:
        state["task_results"] = [{"task_id": "market", "status": "success", "content": "行情"}]
        if case == "budget":
            state["planner_feedback_count"] = MAX_PLANNER_FEEDBACKS
        else:
            state["planner_reviewed_signature"] = observation_signature(
                PlanModel.model_validate(state["plan"]), state["task_results"],
            )
    elif case == "approval":
        state["pending_tool_approval"] = PendingToolApproval(
            task_id="market", call_id="call", step_index=0, request_fingerprint="hash",
            tool_name="test", risk_level="high", purpose="执行工具",
        )
    else:
        state["skill_workflows"] = {"market": {"status": "failed"}}
    original = copy.deepcopy(state)
    with langfuse_agent_run(
        business_trace_id=f"test-branch-{uuid4().hex}", session_key="s", tenant_id="t",
        execution_segment="initial",
    ):
        assert _dispatch_after_scheduler(state) == target
    assert state == original
    runtime.client.flush()
    span = next(span for span in exporter.get_finished_spans() if span.name == "dispatch_task_scheduler")
    output = json.loads(span.attributes[OUTPUT])
    assert output["next_node"] == target
    assert output["reason"] == reason


def test_inactive_telemetry_preserves_send_payload_and_return_type(monkeypatch):
    monkeypatch.setattr(langfuse_observability, "_active_runtime", None)
    state = _state()
    dispatch = _dispatch_after_scheduler(state)
    assert isinstance(dispatch, list)
    assert isinstance(dispatch[0], Send)
    assert dispatch[0].node == "tool_agent"
    assert dispatch[0].arg["user_message"] == state["user_message"]
    assert dispatch[0].arg["web_search_allowed"] is True


@pytest.mark.parametrize("limit", [1000, 2500, 20000])
def test_dispatch_content_is_bounded_and_nested_json_secrets_are_removed(telemetry, limit):
    runtime, exporter = telemetry
    state = _state()
    state["plan"]["tasks"][0]["task_goal"] = json.dumps({
        "goal": "查询行情", "password": "nested-private-value",
    })
    # 只扩大调度上下文，不增加任何实际任务或工具调用。
    state["task_results"] = [
        {"task_id": f"old_{index}", "status": "success", "content": "不应上传的正文"}
        for index in range(200)
    ]
    with langfuse_agent_run(
        business_trace_id=f"test-bounds-{uuid4().hex}", session_key="s", tenant_id="t",
        execution_segment="initial",
    ):
        assert _dispatch_after_scheduler(state)[0].node == "tool_agent"
    runtime.client.flush()
    span = next(span for span in exporter.get_finished_spans() if span.name == "dispatch_task_scheduler")
    assert "nested-private-value" not in span.attributes[INPUT]
    assert "不应上传的正文" not in span.attributes[INPUT]
    assert len(span.attributes[INPUT]) <= limit
    assert len(span.attributes[OUTPUT]) <= limit
    value = json.loads(span.attributes[INPUT])
    assert value["truncated"] is True
    assert len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) <= runtime.content_policy.max_chars


@pytest.mark.parametrize("case", ["user_input", "permission_resume", "skill_ready", "skill_workflow"])
def test_dispatch_confirmation_and_skill_branches(telemetry, case):
    from app.schemas.user_input import (
        UserInputOption,
        UserInputRequest,
        UserInputResponse,
        UserInputSource,
    )
    from app.skills.workflow_contracts import SkillWorkflow, WorkflowStep

    runtime, exporter = telemetry
    state = _state()
    expected_task = "market"
    if case in {"user_input", "permission_resume"}:
        source = UserInputSource(
            node="tool_agent", kind="provider_permission_fallback", task_id="market", call_id="call",
        )
        if case == "user_input":
            state["pending_user_input"] = UserInputRequest(
                interaction_id="interaction", input_type="selection", question="private-question",
                options=[UserInputOption(value="accept", label="确认"),
                         UserInputOption(value="decline", label="取消")], source=source,
            )
            target, reason = "user_input_gate", "user_input_required"
        else:
            state["user_input_response"] = UserInputResponse(
                interaction_id="interaction", input_type="selection", value="accept", source=source,
            )
            target, reason = "tool_agent", "permission_fallback_resume"
    else:
        state["plan"]["tasks"][0]["skill_name"] = "research"
        target, reason = "skill_agent", "skill_task_ready"
        if case == "skill_workflow":
            state["skill_workflows"] = {"market": SkillWorkflow(
                parent_task_id="market", skill_name="research", skill_version="1",
                definition_hash="hash", summary="研究", output_requirement="结论",
                steps=[WorkflowStep(task_id="market_step_1", instruction="查询行情",
                                    source="private-workflow-source", tool_name="daily")],
            ).model_dump()}
            expected_task = "market_step_1"
            target, reason = "tool_agent", "skill_workflow_dispatch"
    original = copy.deepcopy(state)
    with langfuse_agent_run(
        business_trace_id=f"test-priority-{uuid4().hex}", session_key="s", tenant_id="t",
        execution_segment="initial",
    ):
        route = _dispatch_after_scheduler(state)
    assert state == original
    assert (route if isinstance(route, str) else route[0].node) == target
    runtime.client.flush()
    span = next(span for span in exporter.get_finished_spans() if span.name == "dispatch_task_scheduler")
    output = json.loads(span.attributes[OUTPUT])
    assert output["reason"] == reason
    assert output["next_node"] == target
    if case != "user_input":
        assert output["dispatched_tasks"][0]["task_id"] == expected_task
    assert "private-question" not in json.dumps(dict(span.attributes))
    assert "private-workflow-source" not in json.dumps(dict(span.attributes))


@pytest.mark.parametrize("failure", ["input_projection", "update", "span_start"])
def test_telemetry_failures_do_not_change_dispatch(telemetry, monkeypatch, failure):
    from app.operations import langfuse_scheduler

    runtime, _ = telemetry
    state = _state()
    original = copy.deepcopy(state)

    def fail(*_, **__):
        raise ValueError("synthetic telemetry failure")

    with langfuse_agent_run(
        business_trace_id=f"test-isolation-{uuid4().hex}", session_key="s", tenant_id="t",
        execution_segment="initial",
    ):
        if failure == "input_projection":
            monkeypatch.setattr(langfuse_scheduler, "scheduler_trace_input", fail)
        elif failure == "update":
            monkeypatch.setattr(langfuse_observability, "update_langfuse_observation", fail)
        else:
            monkeypatch.setattr(runtime.client, "start_as_current_observation", fail)
        route = _dispatch_after_scheduler(state)
        assert route[0].node == "tool_agent"
    assert state == original


def test_parallel_dispatch_reports_nodes_and_task_ids_without_full_send_args(telemetry):
    runtime, exporter = telemetry
    state = _state()
    state["plan"]["tasks"].append({
        "id": "explain", "task_goal": "解释概念", "execution_type": "analysis",
    })
    with langfuse_agent_run(
        business_trace_id=f"test-parallel-{uuid4().hex}", session_key="s", tenant_id="t",
        execution_segment="initial",
    ):
        route = _dispatch_after_scheduler(state)
    assert [send.node for send in route] == ["tool_agent", "analysis_agent"]
    runtime.client.flush()
    span = next(span for span in exporter.get_finished_spans() if span.name == "dispatch_task_scheduler")
    output = json.loads(span.attributes[OUTPUT])
    assert output["next_node"] is None
    assert output["next_nodes"] == ["tool_agent", "analysis_agent"]
    assert [item["task_id"] for item in output["dispatched_tasks"]] == ["market", "explain"]
    assert "private-" not in span.attributes[OUTPUT]
