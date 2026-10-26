"""规划草案采用、回退及历史契约兼容；无真实远端调用。"""

import json
from hashlib import sha256
from types import SimpleNamespace

import pytest
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.planning.contracts import PlanModel, SubTaskModel
from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities
from app.graph.planning.policy import observation_signature
from app.tests.integration.test_planning_candidates_graph import StockTool
from app.tests.unit.test_durable_turns import database  # noqa: F401
from app.tests.unit.test_planning_candidates import QUERY, candidate_state
from app.tests.unit.test_recovery_durability import control as durable_control  # noqa: F401
from app.tests.unit.test_request_flow import Answers
from app.tools.candidate_resolver import ToolCandidateResolver
from app.tools.planning_candidates import recall_candidates
from app.tools.registry import ToolRegistry
from app.tools.search import ToolSearchService
from app.tools.search.index import ToolSearchIndex
from langchain_core.messages import AIMessage

REF = "tool:mcp.tushare.stock_basic@1.0.0"
ARGS = {"name": "银之杰", "fields": ["name", "list_status"]}


def task_data(**extra):
    return {"id": "facts", "task_goal": QUERY, "tool_candidate_refs": [REF],
            "proposed_tool_call": {"candidate_ref": REF, "arguments": ARGS}, **extra}


def setup(*, task_changes=None, tool_risk="low", permission_scopes=()):
    calls, selections = [], []
    tool = StockTool(calls)
    tool.risk_level = tool_risk
    tool.server_injected_fields = ["private_config"]
    tool.permission_scope = list(permission_scopes)
    registry = ToolRegistry()
    registry.register(tool)
    snapshot = registry.snapshot()
    service = ToolSearchService(registry=snapshot, index=ToolSearchIndex.build(
        specs=snapshot.specs(), catalog_digest=snapshot.catalog_digest), candidate_k=20)
    state = candidate_state()
    state["permission_scopes"] = list(permission_scopes)
    state["tool_catalog_digest"] = snapshot.catalog_digest
    state["planning_tool_candidates"] = recall_candidates(
        state=state, search_service=service, top_k=5).model_dump(mode="json")
    state["active_task"] = SubTaskModel.model_validate(task_data(**(task_changes or {}))).model_dump()
    state["planner_iteration"] = 1

    class Client(Answers):
        def bind_tools(self, schemas, *, tool_choice):
            selections.append(schemas)
            return SimpleNamespace(invoke=lambda messages: AIMessage(content="", tool_calls=[{
                "id": "model-id", "name": schemas[0]["function"]["name"], "args": ARGS,
            }]))

    node = build_tool_agent_node(registry_factory=lambda: snapshot,
                               llm_client=Client("没有可用工具，原接口资料未获取。"),
                               candidate_resolver=ToolCandidateResolver(search_service=service))
    return node, state, calls, selections


@pytest.mark.parametrize("risk", ["low", "medium"])
def test_valid_proposal_executes_without_selection_model(risk):
    node, state, calls, selections = setup(tool_risk=risk)
    result = node(state)
    assert calls == [("mcp.tushare.stock_basic", ARGS)] and selections == []
    assert result["task_results"][0]["status"] == "success"
    assert next(iter(result["tool_discovery_events"].values()))["proposal_status"] == "accepted"
    assert result["tool_calls"][0].request.call_id != "model-id"


@pytest.mark.parametrize("changes", [
    {"depends_on": ["prior"]}, {"proposed_tool_call": None},
    {"proposed_tool_call": {"candidate_ref": REF, "arguments": {"name": 42}}},
    {"proposed_tool_call": {"candidate_ref": "tool:unknown@1", "arguments": ARGS}},
    {"proposed_tool_call": {"candidate_ref": REF, "arguments": {**ARGS, "private_config": "hidden"}}},
])
def test_unusable_proposal_falls_back_before_one_actual_dispatch(changes):
    node, state, calls, selections = setup(task_changes=changes)
    result = node(state)
    assert len(selections) == 1 and calls == [("mcp.tushare.stock_basic", ARGS)]
    assert result["task_results"][0]["status"] == "success"


def test_undisclosed_schema_does_not_enable_direct_execution():
    node, state, calls, selections = setup()
    state["planning_tool_candidates"]["candidates"][0].pop("input_schema")
    node(state)
    assert len(selections) == 1 and len(calls) == 1


def test_permission_revocation_cannot_be_bypassed_by_a_proposal():
    node, state, calls, selections = setup(permission_scopes=("market:read",))
    state["permission_scopes"] = []
    result = node(state)
    assert calls == [] and selections == []
    assert "资料未获取" in result["task_results"][0]["content"]
    assert next(iter(result["tool_discovery_events"].values()))["proposal_status"] == "candidate_unavailable"


def test_catalog_change_stops_before_proposal_or_model_dispatch():
    from app.tools.registry import ToolCatalogChangedError

    node, state, calls, selections = setup()
    state["tool_catalog_digest"] = "changed"
    with pytest.raises(ToolCatalogChangedError):
        node(state)
    assert calls == selections == []


def test_task_snapshot_does_not_ignore_changed_proposal_arguments():
    from app.graph.planning.contracts import task_snapshot_matches

    task = SubTaskModel.model_validate(task_data())
    changed = task_data(proposed_tool_call={"candidate_ref": REF, "arguments": {**ARGS, "name": "另一家公司"}})
    assert not task_snapshot_matches(changed, task)


def test_high_risk_tool_keeps_existing_selection_and_approval():
    node, state, calls, selections = setup(tool_risk="high")
    result = node(state)
    assert len(selections) == 1
    assert calls == [] and result["pending_tool_approval"] is not None


@pytest.mark.parametrize("phase", ["running", "unknown"])
def test_medium_proposal_still_obeys_unresolved_effect_guard(request, phase):
    from app.conversations.execution import execution_control
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt
    from sqlalchemy import select

    control = request.getfixturevalue("durable_control")
    node, state, calls, selections = setup(tool_risk="medium")
    previous = control.tool_started(ToolCallRequest(call_id="previous", tool_name="other",
        arguments={}, purpose="前序操作"), risk_level="medium")
    if phase == "unknown":
        control.tool_finished(previous, {"status": "unknown"}, uncertain=True)
    with execution_control(control):
        result = node(state)
    assert calls == selections == []
    assert next(iter(result["tool_discovery_events"].values()))["proposal_status"] == "accepted"
    assert result["tool_calls"][0].result.error.code == "tool_side_effects_unresolved"
    assert result["tool_calls"][0].result.execution_state == "not_started"
    with control.session_factory() as db:
        receipts = list(db.scalars(select(AgentRunToolReceipt).where(
            AgentRunToolReceipt.run_id == control.run_id)))
        assert len(receipts) == 1 and receipts[0].status == phase


def test_absent_proposal_preserves_legacy_signature_and_task_snapshot():
    from app.graph.planning.contracts import task_snapshot_matches

    legacy = {"id": "facts", "task_goal": "取数"}
    task = SubTaskModel.model_validate(legacy)
    assert task.proposed_tool_call is None and task_snapshot_matches(legacy, task)
    plan = PlanModel(tasks=[task])
    payload = plan.model_dump()
    payload["tasks"][0].pop("tool_candidate_refs")
    payload["tasks"][0].pop("proposed_tool_call")
    expected = sha256(json.dumps({"plan": payload, "results": []},
                                 sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert observation_signature(plan, []) == expected


def test_malformed_or_credential_proposal_does_not_cost_planner_repair():
    for proposal in ({"candidate_ref": REF, "arguments": {"token": "synthetic-secret"}},
                     {"candidate_ref": REF, "arguments": "not-an-object"}):
        llm = Answers({"tasks": [task_data(tool_candidate_refs=[], proposed_tool_call=proposal)]})
        result = LlmTaskPlanner(llm_client=llm).plan(goal=QUERY, intent_type="financial_research",
            domain="a_share_equity_research", subdomain="general_research",
            capabilities=PlannerCapabilities(), task_results=(), remaining_iterations=2)
        assert result.tasks[0].proposed_tool_call is None and len(llm.calls) == 1


@pytest.mark.parametrize("prompt_name", ["_SYSTEM_PROMPT", "_FEEDBACK_PROMPT"])
def test_proposal_prompt_examples_survive_existing_trace_redaction(prompt_name):
    from app.graph.planning import planner
    from app.operations.langfuse_content import LangfuseContentPolicy
    from app.operations.langfuse_langchain_content import sanitized_content_json

    prompt = getattr(planner, prompt_name)
    sanitized = sanitized_content_json({"content": prompt},
        policy=LangfuseContentPolicy(max_chars=20000))
    assert "proposed_tool_call" in json.loads(sanitized)["content"]


def test_changing_only_proposal_does_not_allow_renaming_failed_work():
    from app.graph.planning.contracts import TaskResultModel
    from app.graph.planning.planner import PlannerOutputError
    from app.tests.unit.test_planning_candidates import stock_spec
    from app.tools.planning_candidates import project_card

    current = PlanModel.model_validate({"tasks": [task_data()]})
    changed = task_data(id="renamed", proposed_tool_call={"candidate_ref": REF,
                                                         "arguments": {**ARGS, "name": "另一家公司"}})
    llm = Answers({"action": "revise", "reason": "只换参数和 ID", "plan": {"tasks": [changed]}})
    with pytest.raises(PlannerOutputError):
        LlmTaskPlanner(llm_client=llm).review(goal=QUERY, user_message=QUERY, current_plan=current,
            task_results=(TaskResultModel(task_id="facts", status="business_failure", content="权限不足"),),
            capabilities=PlannerCapabilities(tools=(project_card(stock_spec()),)),
            remaining_revisions=1, replan_request={"reason_type": "missing_task"})
