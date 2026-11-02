"""队列串行执行、参数修正优先、成功剪枝；不把备用调用注入真实对话。"""

import json
from types import SimpleNamespace
from typing import ClassVar

import pytest
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.state import create_initial_state, merge_task_results, merge_tool_call_records
from app.schemas.tools import ToolCallResult, ToolError
from app.tests.unit.test_multi_source_plan import plan_reply
from app.tools.base import Tool
from app.tools.candidate_resolver import ToolCandidateResolution
from app.tools.registry import ExecutionBinding, ToolRegistry
from app.tools.web_search import WebSearchInput
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel


class Input(BaseModel):
    year: int


class Source(Tool[Input]):
    version = "1"
    input_model = Input
    effect_type = "read_only"

    def __init__(self, name, codes):
        self.name, self.codes, self.calls = name, list(codes), []

    async def run(self, data):
        self.calls.append(data.year)
        code = self.codes.pop(0) if self.codes else None
        return ToolCallResult(tool_call_id="remote", tool_name=self.name, trace_id="",
            status="failed" if code else "success", execution_state="not_started" if code else "completed",
            error=ToolError(code=code, message="提供方错误", details={"expected_year": 2024}) if code else None,
            result=None if code else getattr(self, "result_payload", {"income": 0}))


class Search(Tool[WebSearchInput]):
    name, version, input_model, effect_type = "web_search", "1", WebSearchInput, "read_only"
    permission_scope: ClassVar[list[str]] = ["web:search"]

    def __init__(self):
        self.calls = []

    async def run(self, data):
        self.calls.append(data.query)
        return ToolCallResult(tool_call_id="search", tool_name=self.name, trace_id="", status="success",
            result=getattr(self, "result_payload", {"query": data.query, "answer": "公开资料", "results": []}))


class Model:
    def __init__(self):
        self.calls, self.messages = [], []

    def bind_tools(self, tools, *, tool_choice):
        names = [tool["function"]["name"] for tool in tools]
        self.calls.append(names)

        def invoke(messages):
            self.messages.append(messages)
            if names != ["plan_source_calls"]:
                return AIMessage(content="", tool_calls=[{"id": "repair", "name": names[0], "args": {"year": 2024}}])
            return plan_reply(tools[0], [
                {"id": "a", "name": "mcp_alpha_annual", "args": {"year": 2023}},
                {"id": "b", "name": "mcp_beta_annual", "args": {"year": 2024}},
                {"id": "w", "name": "web_search", "args": {"query": "公司2024年营业收入"}},
            ])
        return SimpleNamespace(invoke=invoke)


def setup(codes_a=(), codes_b=(), risk="low", search_budget=None):
    a, b, search = Source("mcp.alpha.annual", codes_a), Source("mcp.beta.annual", codes_b), Search()
    a.risk_level = risk
    registry = ToolRegistry()
    for tool in (a, b):
        registry.register(tool, execution_binding=ExecutionBinding(provider="mcp", server_id=tool.name.split(".")[1],
            implementation_id=tool.name, deployment_revision="1"))
    registry.register(search)
    registry = registry.snapshot()
    resolver = SimpleNamespace(resolve=lambda **kwargs: ToolCandidateResolution(
        tuple(registry.get(tool.name).spec for tool in (a, b, search)), (), "auto", None, multi_source=True))
    model = Model()
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="查询公司2024年营业收入",
        permission_scopes=["web:search"], tool_catalog_digest=registry.catalog_digest)
    state.update(request_flow_version="context-routing-v1", planner_iteration=1,
        plan={"tasks": [{"id": "facts", "task_goal": "查询公司2024年营业收入"}]},
        active_task={"id": "facts", "task_goal": "查询公司2024年营业收入"})
    node = build_tool_agent_node(registry_factory=lambda: registry, llm_client=model, candidate_resolver=resolver, search_budget=search_budget)
    return node, state, a, b, search, model


def apply(state, update):
    state["tool_calls"] = merge_tool_call_records(state["tool_calls"], update.get("tool_calls", []))
    state["task_results"] = merge_task_results(state["task_results"], update.get("task_results", []))
    state["messages"] = add_messages(state["messages"], update.get("messages", []))
    if "pending_tool_approval" in update:
        state["pending_tool_approval"] = update["pending_tool_approval"]
    for key in ("source_attempt_queues", "tool_selection_contexts", "tool_discovery_events", "tool_documentation"):
        state[key] = {**state.get(key, {}), **update.get(key, {})}


def finish(node, state):
    for _ in range(12):
        update = node(state)
        apply(state, update)
        if state["task_results"]:
            return state
    raise AssertionError("备用调用未收敛")


def test_first_success_prunes_other_calls_and_only_executed_calls_enter_messages():
    node, state, a, b, search, model = setup()
    result = finish(node, state)
    assert a.calls == [2023] and b.calls == search.calls == []
    assert len(model.calls) == 1
    assert result["task_results"][0]["tool_name"] == a.name
    assert len(result["tool_calls"]) == 1
    assert sum(isinstance(message, ToolMessage) for message in result["messages"]) == 1
    queue = result["source_attempt_queues"]["facts"]
    assert queue["status"] == "succeeded"
    assert [item["status"] for item in queue["calls"]] == ["succeeded", "pruned", "pruned"]


def test_permission_failure_switches_to_precomputed_next_source_without_model_call():
    node, state, a, b, search, model = setup(["provider_permission_denied"])
    result = finish(node, state)
    assert a.calls == [2023] and b.calls == [2024] and search.calls == []
    assert len(model.calls) == 1
    assert result["task_results"][0]["tool_name"] == b.name
    assert len(result["tool_calls"]) == 2
    assert not result.get("pending_user_input")


@pytest.mark.parametrize("second_error", [None, "provider_invalid_request"])
def test_parameter_failure_repairs_same_tool_before_switching_sources(second_error):
    node, state, a, b, search, model = setup(["provider_invalid_request", second_error])
    result = finish(node, state)
    assert a.calls == [2023, 2024] and len(model.calls) == 2
    assert model.calls[1] == ["mcp_alpha_annual"]
    assert bool(b.calls) == bool(second_error)
    assert search.calls == []
    assert result["tool_calls"][0].request.call_id != result["tool_calls"][1].request.call_id
    assert "expected_year" in json.dumps([message.content for message in model.messages[1]])


def test_all_professional_sources_fail_then_search_executes_precomputed_arguments():
    node, state, a, b, search, model = setup(["provider_request_rejected"], ["mcp_tool_timeout"])
    result = finish(node, state)
    assert a.calls == [2023] and b.calls == [2024]
    assert search.calls == ["公司2024年营业收入"] and len(model.calls) == 1
    assert result["task_results"][0]["tool_name"] == "web_search"
    assert len(result["tool_calls"]) == 3


def test_recreated_node_resumes_saved_queue_without_regenerating_parameters():
    import copy

    node, state, a, b, search, model = setup(["provider_permission_denied"])
    apply(state, node(state))  # 只生成计划，尚未执行。
    assert model.calls and a.calls == b.calls == []
    apply(state, node(state))  # A 失败，下一位置已保存。
    state = copy.deepcopy(state)
    finish(node, state)
    assert a.calls == [2023] and b.calls == [2024] and search.calls == []
    assert len(model.calls) == 1


def test_switch_progress_replaces_same_activity_without_final_failure(monkeypatch):
    from app.graph import public_events

    events = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: events.append)
    node, state, a, b, _, _ = setup(["provider_permission_denied"])
    apply(state, node(state))
    public_events.emit_public_progress("tool_agent", state, status="completed")
    apply(state, node(state))
    public_events.emit_public_progress("tool_agent", state, status="completed")
    assert events[-1]["status"] == "started"
    assert "alpha未取得数据" in events[-1]["message"] and "正在尝试beta" in events[-1]["message"]
    finish(node, state)
    public_events.emit_public_progress("tool_agent", state, status="completed")
    assert len({event["id"] for event in events}) == 1
    assert events[-1]["status"] == "completed"
    assert "已从beta取得数据" == events[-1]["message"].rstrip("。")
    assert a.calls == [2023] and b.calls == [2024]


def test_expired_task_snapshot_does_not_execute_or_regenerate_plan():
    node, state, a, b, search, model = setup()
    apply(state, node(state))
    state["active_task"]["task_goal"] = "另一个问题"
    result = node(state)
    assert result["task_results"][0]["error_code"] == "source_plan_invalid"
    assert a.calls == b.calls == search.calls == [] and len(model.calls) == 1


def test_parameter_repair_does_not_replay_identical_arguments():
    node, state, a, b, _, model = setup(["provider_invalid_request"])
    original_bind = model.bind_tools

    def bind(tools, *, tool_choice):
        if tools[0]["function"]["name"] == "plan_source_calls":
            return original_bind(tools, tool_choice=tool_choice)
        model.calls.append([tools[0]["function"]["name"]])
        return SimpleNamespace(invoke=lambda messages: AIMessage(content="", tool_calls=[{
            "id": "duplicate", "name": tools[0]["function"]["name"], "args": {"year": 2023},
        }]))

    model.bind_tools = bind
    finish(node, state)
    assert a.calls == [2023] and b.calls == [2024]
    assert state["source_attempt_queues"]["facts"]["calls"][0]["repair_errors"][0]["code"] == "argument_repair_failed"


def test_all_candidates_fail_and_original_errors_are_kept_without_replan():
    from app.graph.planning.request_flow import replan_block_reason

    node, state, a, b, search, model = setup(["provider_permission_denied"], ["mcp_tool_timeout"])
    state["permission_scopes"] = []  # 保留真实网关权限校验，搜索不会派发。
    finish(node, state)
    assert a.calls == [2023] and b.calls == [2024] and search.calls == []
    assert len(model.calls) == 1 and len(state["tool_calls"]) == 3
    assert state["source_attempt_queues"]["facts"]["status"] == "failed"
    assert state["task_results"][0]["status"] != "success"
    assert replan_block_reason(state) == "source_plan_exhausted"


def test_approval_waits_without_executing_next_source():
    from app.graph.planning.request_flow import replan_block_reason
    from app.schemas.approvals import ApprovedToolCall
    from app.tools.fingerprints import tool_call_fingerprint

    node, state, a, b, search, model = setup(["provider_permission_denied"], risk="high")
    apply(state, node(state))
    apply(state, node(state))
    assert state["pending_tool_approval"] is not None
    assert state["source_attempt_queues"]["facts"]["status"] == "waiting"
    assert replan_block_reason(state) == "replan_pending_execution"
    assert a.calls == b.calls == search.calls == [] and len(model.calls) == 1
    request = state["tool_calls"][0].request
    state["approved_tool_calls"] = [ApprovedToolCall(call_id=request.call_id,
        arguments_hash=tool_call_fingerprint(request))]
    state["pending_tool_approval"] = None
    finish(node, state)
    assert a.calls == [2023] and b.calls == [2024] and search.calls == []
    assert len(model.calls) == 1


def test_invalid_search_parameters_do_not_consume_budget_needed_for_correction():
    from app.tools.call_budget import SearchCallBudget

    node, state, _a, _b, search, model = setup(["provider_request_rejected"], ["mcp_tool_timeout"],
        search_budget=SearchCallBudget(limit=1))
    original = model.bind_tools

    def bind(tools, *, tool_choice):
        if tools[0]["function"]["name"] != "plan_source_calls":
            model.calls.append(["web_search"])
            return SimpleNamespace(invoke=lambda messages: AIMessage(content="", tool_calls=[{
                "id": "repair", "name": "web_search", "args": {"query": "公司2024年营业收入"},
            }]))
        bound = original(tools, tool_choice=tool_choice)

        def initial(messages):
            response = bound.invoke(messages)
            response.tool_calls[0]["args"]["choices"]["source_2"]["arguments"]["query"] = "长" * 129
            return response
        return SimpleNamespace(invoke=initial)

    model.bind_tools = bind
    finish(node, state)
    assert search.calls == ["公司2024年营业收入"]
    assert state["task_results"][0]["status"] == "success"
    assert len(model.calls) == 2
    assert state["tool_calls"][-2].result.error.code == "tool_argument_validation_failed"
    assert state["tool_calls"][-2].result.execution_state == "not_started"


@pytest.mark.parametrize("empty", [[], {}, None, "", " \n\t"])
def test_empty_success_advances_to_next_source_without_argument_repair(empty):
    node, state, a, b, search, model = setup()
    a.result_payload = empty
    finish(node, state)

    assert a.calls == [2023] and b.calls == [2024] and search.calls == []
    assert len(model.calls) == 1
    assert state["task_results"][0]["tool_name"] == b.name
    queue = state["source_attempt_queues"]["facts"]
    assert [call["status"] for call in queue["calls"]] == ["failed", "succeeded", "pruned"]
    assert queue["calls"][0]["last_error"]["code"] == "tool_result_empty"
    assert queue["calls"][0]["repair_count"] == 0
    receipt = state["tool_calls"][0].result
    assert receipt.status == "success" and receipt.error is None and receipt.result == empty


@pytest.mark.parametrize("data", [0, False, {"income": 0}, [0], {"data": []}])
def test_nonempty_or_scalar_success_is_not_semantically_validated(data):
    node, state, a, b, search, model = setup()
    a.result_payload = data
    finish(node, state)
    assert a.calls == [2023] and b.calls == search.calls == []
    assert state["source_attempt_queues"]["facts"]["status"] == "succeeded"
    assert len(model.calls) == 1


def test_empty_professional_sources_fall_back_to_precomputed_search():
    node, state, a, b, search, model = setup()
    a.result_payload, b.result_payload = [], {}
    finish(node, state)
    assert a.calls == [2023] and b.calls == [2024]
    assert search.calls == ["公司2024年营业收入"]
    assert len(model.calls) == 1
    assert state["task_results"][0]["tool_name"] == "web_search"


def test_empty_sources_exhaust_queue_without_claiming_data_success_or_replanning():
    from app.graph.planning.request_flow import replan_block_reason

    node, state, a, b, search, model = setup()
    a.result_payload, b.result_payload = [], None
    search.result_payload = {"query": "公司2024年营业收入", "answer": "", "results": [],
                             "coverage": {"status": "unknown"}}
    finish(node, state)
    assert len(state["tool_calls"]) == 3 and len(model.calls) == 1
    assert all(record.result.status == "success" and record.result.error is None for record in state["tool_calls"])
    assert state["source_attempt_queues"]["facts"]["status"] == "failed"
    assert state["task_results"][0]["status"] == "business_failure"
    assert state["task_results"][0]["error_code"] == "tool_result_empty"
    assert replan_block_reason(state) == "source_plan_exhausted"


def test_empty_source_switch_progress_remains_in_progress_and_does_not_finalize_task(monkeypatch):
    from app.graph import public_events

    events = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: events.append)
    node, state, a, _b, _search, _model = setup()
    a.result_payload = []
    apply(state, node(state))
    apply(state, node(state))
    public_events.emit_public_progress("tool_agent", state, status="completed")
    assert state["task_results"] == []
    assert events[-1]["status"] == "started"
    assert "alpha未取得数据，正在尝试beta" in events[-1]["message"]
