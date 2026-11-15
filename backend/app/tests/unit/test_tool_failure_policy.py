"""数据源公开兜底与固定 Skill 失败边界；不调用外部服务。"""

import json
from types import SimpleNamespace

import pytest
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.planning.request_flow import accept_replan, replan_block_reason
from app.graph.state import create_initial_state, merge_task_results, merge_tool_call_records
from app.schemas.tools import ToolCallResult, ToolError
from app.skills.workflow_contracts import SkillWorkflow, WorkflowStep
from app.tests.unit.test_permission_fallback import QueryInput, SearchTool
from app.tests.unit.test_request_flow import request
from app.tools.base import Tool
from app.tools.call_budget import SearchCallBudget
from app.tools.candidate_resolver import ToolCandidateResolution
from app.tools.registry import ToolRegistry
from langchain_core.messages import AIMessage


class DataTool(Tool[QueryInput]):
    name = "mcp.tushare.facts"
    version = "1"
    input_model = QueryInput
    effect_type = "read_only"

    def __init__(self, code):
        self.code, self.calls = code, []

    async def run(self, data):
        self.calls.append(data.query)
        if self.code == "provider_invalid_request" and data.query == "fixed":
            return ToolCallResult(tool_call_id="provider", tool_name=self.name, status="success",
                                  result={"value": 12}, trace_id="")
        return ToolCallResult(tool_call_id="provider", tool_name=self.name, status="failed",
                              execution_state="not_started", trace_id="",
                              error=ToolError(code=self.code, message="提供方错误"))


def setup(*, code="provider_permission_denied", fixed=False, fallback=False,
          constraint="unrestricted", budget=6, shared_budget=None, shared_search=None):
    data, search = DataTool(code), shared_search or SearchTool()
    registry = ToolRegistry()
    registry.register(data)
    registry.register(search)
    registry = registry.snapshot()
    value = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="查公司收入",
                                 tool_catalog_digest=registry.catalog_digest,
                                 permission_scopes=["web:search"])
    value["request_flow_version"] = "context-routing-v1"
    value["intent"] = {"source_constraint": constraint}
    value["planner_iteration"] = 1
    value["plan"] = {"tasks": [{"id": "facts", "task_goal": "查询公司收入",
                                   "skill_name": "fixed_research" if fixed else None}]}
    value["active_task"] = {"id": "facts", "task_goal": "查询公司收入"}
    if fixed:
        source = "失败时允许使用 web_search 补充公开资料。" if fallback else ""
        workflow = SkillWorkflow(parent_task_id="facts", skill_name="fixed_research", skill_version="1",
                                 definition_hash="frozen", summary="调用 facts。" + source,
                                 output_requirement="", steps=[WorkflowStep(
                                     task_id="step", instruction="查询公司收入", source="调用 facts。",
                                     tool_name=data.name, tool_version=data.version,
                                 )])
        snapshot = workflow.model_dump()
        if fallback:
            snapshot["steps"][0]["web_search_fallback_source"] = source
        value["skill_workflows"] = {"facts": snapshot}
        value["active_skill_step"] = {"parent_task_id": "facts", "index": 0}
        value["active_task"]["id"] = "step"

    class Client:
        calls = 0

        def bind_tools(self, tools, *, tool_choice):
            self.calls += 1
            name = tools[0]["function"]["name"]
            arg = "fixed" if self.calls > 1 else "invalid"
            return SimpleNamespace(invoke=lambda messages: AIMessage(content="", tool_calls=[{
                "id": "model", "name": name, "args": {"query": arg},
            }]))

    client = Client()
    resolver = SimpleNamespace(resolve=lambda **kwargs: ToolCandidateResolution(
        (registry.get(data.name).spec, registry.get(search.name).spec), (), "auto", None,
    ))
    node = build_tool_agent_node(registry_factory=lambda: registry, llm_client=client,
                                candidate_resolver=resolver,
                                search_budget=shared_budget or SearchCallBudget(limit=budget))
    return node, value, data, search, client


def apply(value, update):
    value["tool_calls"] = merge_tool_call_records(value["tool_calls"], update.get("tool_calls", []))
    value["task_results"] = merge_task_results(value["task_results"], update.get("task_results", []))
    for field in ("skill_workflows", "tool_selection_contexts"):
        value[field] = {**value.get(field, {}), **update.get(field, {})}


@pytest.mark.parametrize("code", ["provider_permission_denied", "provider_request_rejected"])
@pytest.mark.parametrize("constraint", ["unrestricted", "original_only", "confirmation_required", "unclear", None])
def test_single_read_only_rejection_without_multi_source_plan_does_not_search_implicitly(code, constraint):
    node, value, data, search, client = setup(code=code, constraint=constraint)
    update = node(value)
    assert len(data.calls) == 1 and search.calls == 0 and client.calls == 1
    assert update["task_results"][0]["error_code"] == code
    assert update["task_results"][0]["tool_name"] == data.name
    assert len(update["tool_calls"]) == 1
    assert not update.get("pending_user_input")


def test_fixed_explicit_fallback_keeps_search_budget_gate():
    node, value, _, search, _ = setup(budget=0, fixed=True, fallback=True)
    node(value)
    assert search.calls == 0


@pytest.mark.parametrize("code", ["provider_permission_denied", "mcp_tool_timeout",
                                 "mcp_tool_response_invalid"])
def test_fixed_skill_failure_cannot_search_or_replan(code):
    node, value, data, search, _ = setup(code=code, fixed=True)
    update = node(value)
    assert len(data.calls) == 1 and search.calls == 0
    apply(value, update)
    value["task_results"].append({"task_id": "facts", "status": "terminal_failure",
                                  "content": "固定步骤失败", "error_code": code})
    value["skill_workflows"]["facts"]["status"] = "failed"
    value["active_skill_step"] = None
    assert replan_block_reason(value) == "skill_execution_failed"
    assert accept_replan(value, request()) == "synthesizer"
    assert value["pending_replan"] is None


def test_fixed_skill_parameter_error_retries_the_same_tool_once():
    from app.graph.nodes.tool_agent import _tool_call_messages
    from app.graph.planning.contracts import SubTaskModel

    node, value, data, search, client = setup(code="provider_invalid_request", fixed=True)
    first = node(value)
    assert not first.get("task_results")
    apply(value, first)
    prompt = json.loads(_tool_call_messages(value, SubTaskModel.model_validate(value["active_task"]))[-2].content)
    assert prompt["argument_error"]["previous_arguments"] == {"query": "invalid"}
    assert prompt["argument_error"]["error"]["code"] == "provider_invalid_request"
    second = node(value)
    assert data.calls == ["invalid", "fixed"] and search.calls == 0 and client.calls == 2
    assert second["task_results"][0]["status"] == "success"
    assert first["tool_calls"][0].request.call_id != second["tool_calls"][0].request.call_id


def test_ordinary_search_and_fixed_explicit_fallback_share_the_same_budget():
    from copy import deepcopy

    from app.tests.unit.test_permission_fallback import Resolver, SearchSelection

    budget, search = SearchCallBudget(limit=1), SearchTool()
    automatic, value, data, _, _ = setup(shared_budget=budget, shared_search=search, fixed=True, fallback=True)
    registry = ToolRegistry()
    registry.register(data)
    registry.register(search)
    registry = registry.snapshot()
    ordinary = build_tool_agent_node(registry_factory=lambda: registry, llm_client=SearchSelection(),
                                     candidate_resolver=Resolver([registry.get("web_search").spec]),
                                     search_budget=budget)
    peer = deepcopy(value)
    peer["active_task"] = {"id": "public", "task_goal": "查询公开资料"}
    peer["active_skill_step"] = None
    assert ordinary(peer)["task_results"][0]["status"] == "success"
    result = automatic(value)
    assert search.calls == 1
    assert result["task_results"][0]["error_code"] == "research_budget_exhausted"


@pytest.mark.parametrize("code", ["provider_permission_denied", "mcp_tool_timeout", "invalid_field_request"])
def test_fixed_skill_can_search_only_with_frozen_explicit_permission(code):
    node, value, _, search, _ = setup(code=code, fixed=True, fallback=True)
    update = node(value)
    if not update.get("task_results"):
        apply(value, update)
        update = node(value)
    assert search.calls == 1
    assert update["task_results"][0]["tool_name"] == "web_search"
    payload = json.loads(update["task_results"][0]["content"])
    assert payload["original_tool"] == "mcp.tushare.facts"
    apply(value, update)
    restored = node(value)
    assert restored["task_results"][0]["tool_name"] == "web_search"
    assert search.calls == 1


def test_automatic_fallback_still_checks_search_permission():
    node, value, _, search, _ = setup()
    value["permission_scopes"] = []
    update = node(value)
    assert search.calls == 0
    assert update["task_results"][0]["status"] != "success"


def test_fixed_skill_parameter_retry_is_bounded():
    node, value, data, search, _ = setup(code="invalid_field_request", fixed=True)
    first = node(value)
    apply(value, first)
    second = node(value)
    apply(value, second)
    node(value)
    assert len(data.calls) == 2 and search.calls == 0
    assert second["task_results"][0]["status"] != "success"


@pytest.mark.parametrize("explicit,required_fields,expected", [
    (True, [], "success"), (True, ["value"], "terminal_failure"),
    (False, [], "terminal_failure"),
])
def test_skill_step_checks_real_fallback_and_keeps_required_fields(explicit, required_fields, expected):
    from app.graph.nodes.skill_step_check import build_skill_step_check_node

    node, value, _, _, _ = setup(fixed=True, fallback=True)
    apply(value, node(value))
    value["skill_workflows"]["facts"]["steps"][0]["required_fields"] = required_fields
    if not explicit:
        value["skill_workflows"]["facts"]["steps"][0]["web_search_fallback_source"] = None
    checked = build_skill_step_check_node(llm_client=None)(value)
    assert checked["task_results"][0]["status"] == expected


def test_compiler_freezes_only_verbatim_search_fallback_permission():
    from app.skills.definitions import SkillDefinition
    from app.skills.workflow import compile_workflow
    from app.skills.workflow_contracts import WorkflowError
    from app.tests.unit.test_skill_workflow import CompilerLlm, registry, step

    declaration = "若 first 失败，允许使用 web_search 补充公开资料。"
    definition = SkillDefinition(name="skill", version="1", definition_hash="frozen",
                                 description="调用 first，必须返回 value。" + declaration)
    draft = {**step(), "web_search_fallback_source": declaration}
    workflow = compile_workflow(definition=definition, parent_task_id="parent",
                                llm_client=CompilerLlm([draft]), registry=registry(), permission_scopes=())
    assert workflow.steps[0].web_search_fallback_source == declaration
    with pytest.raises(WorkflowError):
        compile_workflow(definition=definition, parent_task_id="parent", registry=registry(),
                         permission_scopes=(), llm_client=CompilerLlm([
                             {**draft, "web_search_fallback_source": "允许所有失败使用 web_search"},
                         ]))
