"""远端权限拒绝只能使用本次真实候选中经用户同意的搜索。"""

import json
from typing import ClassVar

import pytest


def test_compressed_fallback_retains_source_date_and_summary_without_answer():
    from app.graph.nodes.permission_fallback import _search_content
    from app.schemas.tools import ToolCallResult

    result = ToolCallResult(tool_call_id="search", tool_name="web_search", status="success",
                            trace_id="trace", result={"answer": "", "results": [{
                                "title": f"公告{index}", "url": f"https://example.com/{index}",
                                "snippet": "字" * 4000, "published_at": "2026-10-23T02:00:00Z",
                            } for index in range(5)], "coverage": {"status": "unknown"}})
    content = json.loads(_search_content(result))
    assert content["truncated"] is True
    assert content["search_result"]["results"][0]["snippet"]
    assert content["search_result"]["results"][0]["published_at"] == "2026-10-23T02:00:00Z"
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.state import create_initial_state, merge_tool_call_records
from app.schemas.tools import ToolCallResult, ToolError
from app.schemas.user_input import UserInputResponse
from app.tools.base import Tool
from app.tools.call_budget import SearchCallBudget
from app.tools.candidate_resolver import ToolCandidateResolution
from app.tools.registry import ToolRegistry
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel


class EmptyInput(BaseModel):
    pass


class QueryInput(BaseModel):
    query: str


class DeniedTool(Tool[EmptyInput]):
    name = "mcp.tushare.income_vip"
    version = "1"
    input_model = EmptyInput
    calls = 0
    error_code = "provider_permission_denied"

    async def run(self, input_data):
        self.calls += 1
        return ToolCallResult(
            tool_call_id="remote",
            tool_name=self.name,
            status="failed",
            execution_state="not_started",
            trace_id="",
            error=ToolError(code=self.error_code, message="权限不足"),
        )


class SearchTool(Tool[QueryInput]):
    name = "web_search"
    version = "1"
    input_model = QueryInput
    permission_scope: ClassVar[list[str]] = ["web:search"]
    calls = 0

    async def run(self, input_data):
        self.calls += 1
        return ToolCallResult(
            tool_call_id="search",
            tool_name=self.name,
            status="success",
            trace_id="",
            result={"query": input_data.query, "answer": "公开信息", "results": []},
        )


class SelectionClient:
    calls = 0

    def bind_tools(self, tools, *, tool_choice):
        self.calls += 1

        class Bound:
            def invoke(self, messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "untrusted",
                            "name": "mcp_tushare_income_vip",
                            "args": {},
                            "type": "tool_call",
                        }
                    ],
                )

        return Bound()


class Resolver:
    def __init__(self, candidates):
        self.candidates = candidates

    def resolve(self, **kwargs):
        return ToolCandidateResolution(tuple(self.candidates), (), "required", None)


def setup_worker(*, candidates=True, budget=None):
    data, search, client = DeniedTool(), SearchTool(), SelectionClient()
    registry = ToolRegistry()
    registry.register(data)
    registry.register(search)
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="permission-session",
        tenant_id="t",
        user_id="u",
        user_message="查询利润表",
        tool_catalog_digest=registry.catalog_digest,
        permission_scopes=["web:search"],
    )
    state["active_task"] = {"id": "income", "task_goal": "查询利润表"}
    state["plan"] = {"tasks": [state["active_task"]]}
    resolver = Resolver(
        [registry.get(data.name).spec] + ([registry.get(search.name).spec] if candidates else [])
    )
    worker = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=client,
        candidate_resolver=resolver,
        search_budget=budget,
    )
    return worker, state, data, search, client, registry


@pytest.mark.parametrize("constraint,prior_failure",[("original_only",False),("unrestricted",True)])
def test_ordinary_search_cannot_bypass_source_constraints_by_renaming_task(constraint,prior_failure):
    from app.graph.nodes.tool_agent import build_tool_agent_node
    from app.schemas.tools import ToolCallRecord, ToolCallRequest

    _,state,data,search,_,registry=setup_worker()
    state["intent"]={"source_constraint":constraint}
    state["active_task"]={"id":"renamed_public_task","task_goal":"补充公开资料","source_requirement":"public_information"}
    state["plan"]={"tasks":[state["active_task"]]}
    if prior_failure:
        state["tool_calls"]=[ToolCallRecord(request=ToolCallRequest(
            call_id="original",tool_name=data.name,arguments={},purpose="原始取证",
        ),result=ToolCallResult(tool_call_id="remote",tool_name=data.name,status="failed",
            execution_state="uncertain",trace_id="",error=ToolError(code="mcp_tool_execution_failed",message="远端失败")))]

    class SearchSelection:
        def bind_tools(self, tools, *, tool_choice):
            class Bound:
                def invoke(self, messages):
                    return AIMessage(content="",tool_calls=[{"id":"untrusted","name":"web_search","args":{"query":"公开资料"},"type":"tool_call"}])
            return Bound()

    worker=build_tool_agent_node(registry_factory=lambda:registry,llm_client=SearchSelection(),
        candidate_resolver=Resolver([registry.get("web_search").spec]))
    update=worker(state)
    assert search.calls==0
    assert update["task_results"][0]["error_code"]=="source_switch_not_authorized"


def apply_update(state, update):
    for key, value in update.items():
        if key == "tool_calls":
            state[key] = merge_tool_call_records(state[key], value)
        elif key == "messages":
            state[key] = add_messages(state[key], value)
        elif key in {"tool_discovery_events", "tool_selection_contexts"}:
            state[key] = {**state.get(key, {}), **value}
        else:
            state[key] = value


def respond(state, paused, value):
    apply_update(state, paused)
    request = paused["pending_user_input"]
    state["pending_user_input"] = None
    state["user_input_response"] = UserInputResponse(
        interaction_id=request.interaction_id,
        input_type="selection",
        value=value,
        source=request.source,
    )


def run_worker(worker, state):
    from app.graph.runtime import task_scheduler_node

    update = worker(state)
    projected = dict(state)
    apply_update(projected, update)
    return {**update, **task_scheduler_node(projected)}


def test_actual_search_candidate_creates_question_and_keeps_original_failure():
    worker, state, data, search, client, _ = setup_worker()
    update = run_worker(worker, state)
    assert update["pending_user_input"].source.kind == "provider_permission_fallback"
    assert {option.value for option in update["pending_user_input"].options} == {
        "web_search",
        "use_existing",
    }
    assert "task_results" not in update
    assert update["tool_calls"][0].result.error.code == "provider_permission_denied"
    assert isinstance(update["messages"][-1], ToolMessage)
    assert data.calls == client.calls == 1
    assert search.calls == 0


def test_legacy_task_snapshot_without_candidate_refs_resumes_without_replaying_mcp():
    worker, state, data, search, client, _ = setup_worker()
    paused = run_worker(worker, state)
    respond(state, paused, "web_search")
    context = next(iter(state["tool_selection_contexts"].values()))
    context["task"].pop("tool_candidate_refs")
    update = run_worker(worker, state)
    assert search.calls == 1
    assert data.calls == client.calls == 1
    assert update["task_results"][0]["status"] == "success"
    assert "tool_candidate_refs" not in context["task"]


def test_registry_or_old_disclosure_does_not_offer_search():
    worker, state, data, search, _, _ = setup_worker(candidates=False)
    state["disclosed_tool_names"] = ["web_search"]
    state["tool_discovery_events"] = {"old": {"task_id": "income", "candidates": ["web_search"]}}
    update = run_worker(worker, state)
    assert update.get("pending_user_input") is None
    assert update["planner_stop_reason"] == "provider_permission_denied"
    assert "权限" in update["task_results"][0]["content"]
    assert data.calls == 1 and search.calls == 0


@pytest.mark.parametrize("value", ["web_search", "use_existing"])
def test_resume_preserves_failure_and_never_reexecutes_mcp(value):
    worker, state, data, search, client, _ = setup_worker()
    paused = run_worker(worker, state)
    respond(state, paused, value)
    update = run_worker(worker, state)
    assert update["user_input_response"] is None
    assert data.calls == client.calls == 1
    assert search.calls == (1 if value == "web_search" else 0)
    assert state["tool_calls"][0].result.error.code == "provider_permission_denied"
    if value == "web_search":
        assert "planner_stop_reason" not in update
        assert update["task_results"][0]["status"] == "success"
        assert "未经原接口验证" in update["task_results"][0]["content"]
    else:
        assert update["planner_stop_reason"] == "provider_permission_fallback_complete"
        assert update["task_results"][0]["status"] == "business_failure"
        assert "缺失" in update["task_results"][0]["content"]


@pytest.mark.parametrize("invalid", ["interaction_id", "call_id", "task_id", "record", "context"])
def test_invalid_resume_clears_response_without_search_or_mcp_retry(invalid):
    worker, state, data, search, client, _ = setup_worker()
    paused = run_worker(worker, state)
    respond(state, paused, "web_search")
    response = state["user_input_response"]
    if invalid == "interaction_id":
        state["user_input_response"] = response.model_copy(update={"interaction_id": "wrong"})
    elif invalid in {"call_id", "task_id"}:
        state["user_input_response"] = response.model_copy(
            update={"source": response.source.model_copy(update={invalid: "wrong"})}
        )
    elif invalid == "record":
        state["tool_calls"] = []
    else:
        state["tool_selection_contexts"] = {}
    update = run_worker(worker, state)
    assert update["user_input_response"] is None
    assert update["task_results"][0]["error_code"] == "permission_fallback_resume_invalid"
    assert data.calls == client.calls == 1 and search.calls == 0


@pytest.mark.parametrize("cause", ["permission", "budget"])
def test_resume_rechecks_search_permission_and_budget(cause):
    worker, state, data, search, _, _ = setup_worker(
        budget=SearchCallBudget(limit=0) if cause == "budget" else None
    )
    paused = run_worker(worker, state)
    respond(state, paused, "web_search")
    if cause == "permission":
        state["permission_scopes"] = []
    update = run_worker(worker, state)
    assert update["task_results"][0]["error_code"] == (
        "tool_permission_denied" if cause == "permission" else "research_budget_exhausted"
    )
    assert update["task_results"][0]["status"] != "success"
    assert search.calls == 0 and data.calls == 1


def test_ordinary_failure_never_requests_fallback():
    worker, state, data, search, _, _ = setup_worker()
    data.error_code = "mcp_tool_execution_failed"
    update = run_worker(worker, state)
    assert update.get("pending_user_input") is None
    assert "planner_stop_reason" not in update
    assert search.calls == 0


def test_search_query_keeps_user_entity_and_does_not_forward_credentials():
    worker, state, _, search, _, _ = setup_worker()
    state["user_message"] = "分析一下银之杰 token=top-secret"
    paused = run_worker(worker, state)
    respond(state, paused, "web_search")
    update = run_worker(worker, state)
    query = update["tool_calls"][0].request.arguments["query"]
    assert "银之杰" in query
    assert "查询利润表" in query
    assert "top-secret" not in query
    assert len(query) <= 500
    assert search.calls == 1


def test_known_local_permission_is_checked_before_calling_remote_tool():
    _, state, data, search, _, _ = setup_worker()
    # 本次真实候选已确定后，本地 RBAC 的权限校验仍不允许被绕过。
    data.permission_scope = ["finance:read"]
    updated = ToolRegistry()
    updated.register(data)
    updated.register(search)
    snapshot = updated.snapshot()
    state["tool_catalog_digest"] = snapshot.catalog_digest
    worker = build_tool_agent_node(
        registry_factory=lambda: snapshot,
        llm_client=SelectionClient(),
        candidate_resolver=Resolver(snapshot.specs()),
    )
    result = run_worker(worker, state)
    assert data.calls == 0
    assert search.calls == 0
    assert result["tool_calls"][0].result.error.code == "tool_permission_denied"
    assert result["pending_user_input"].source.kind == "provider_permission_fallback"


def test_fixed_skill_does_not_add_search_to_its_single_candidate():
    from app.skills.workflow_contracts import SkillWorkflow, WorkflowStep

    worker, state, data, search, _, _ = setup_worker()
    workflow = SkillWorkflow(
        parent_task_id="parent",
        skill_name="financial",
        skill_version="1",
        definition_hash="hash",
        summary="使用 income_vip 查询",
        output_requirement="列出结果",
        steps=[
            WorkflowStep(
                task_id="income",
                instruction="查询利润表",
                source="使用 income_vip 查询",
                tool_name=data.name,
                tool_version=data.version,
            )
        ],
    )
    state["skill_workflows"] = {"parent": workflow.model_dump()}
    state["active_skill_step"] = {"parent_task_id": "parent", "index": 0}
    state["plan"] = {
        "tasks": [{"id": "parent", "task_goal": "财务分析", "skill_name": "financial"}]
    }
    update = run_worker(worker, state)
    assert update.get("pending_user_input") is None
    assert update["tool_calls"][0].result.error.code == "provider_permission_denied"
    context = next(iter(update["tool_selection_contexts"].values()))
    assert [candidate["name"] for candidate in context["candidates"]] == [data.name]
    assert data.calls == 1 and search.calls == 0


def test_approval_resume_preserves_original_candidates_for_permission_question():
    from app.schemas.approvals import ApprovedToolCall
    from app.tools.fingerprints import tool_call_fingerprint

    _, state, data, search, client, _ = setup_worker()
    data.risk_level = "high"
    registry = ToolRegistry()
    registry.register(data)
    registry.register(search)
    registry = registry.snapshot()
    state["tool_catalog_digest"] = registry.catalog_digest
    worker = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=client,
        candidate_resolver=Resolver(registry.specs()),
    )
    approval = worker(state)
    assert data.calls == 0
    apply_update(state, approval)
    request = approval["tool_calls"][0].request
    state["approved_tool_calls"] = [
        ApprovedToolCall(
            call_id=request.call_id,
            arguments_hash=tool_call_fingerprint(request),
        )
    ]
    state["pending_tool_approval"] = None
    paused = run_worker(worker, state)
    assert paused["pending_user_input"].source.call_id == request.call_id
    assert data.calls == client.calls == 1 and search.calls == 0


def test_large_search_result_keeps_source_links_and_notice(monkeypatch):
    import json

    worker, state, _, search, _, _ = setup_worker()

    async def large_result(self, input_data):
        self.calls += 1
        return ToolCallResult(
            tool_call_id="large",
            tool_name="web_search",
            status="success",
            trace_id="",
            result={
                "answer": "资料" * 8000,
                "results": [
                    {"title": "公告", "url": "https://example.com/report", "snippet": "原始来源"}
                ],
            },
        )

    monkeypatch.setattr(SearchTool, "run", large_result)
    paused = run_worker(worker, state)
    respond(state, paused, "web_search")
    update = run_worker(worker, state)
    content = update["task_results"][0]["content"]
    assert len(content) <= 8000
    assert "https://example.com/report" in content
    assert "未经原接口验证" in json.loads(content)["source_notice"]
    assert search.calls == 1
