"""受控模型经过真实规划、BM25、工具网关与持久化回执，验证多来源切换。"""

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import ClassVar

import pytest
from app.conversations.execution import execution_control
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.runtime import (
    _dispatch_after_scheduler,
    _route_after_synthesis,
    build_task_scheduler_node,
)
from app.graph.state import AgentState, create_initial_state
from app.llm.schemas import LlmCompletionResult
from app.schemas.recovery import RECOVERY_VERSION
from app.schemas.tools import ToolCallResult, ToolError
from app.storage.models import AgentMessage, AgentRun, AgentRunToolReceipt
from app.tests.unit.test_durable_turns import database  # noqa: F401
from app.tests.unit.test_multi_source_plan import plan_reply
from app.tests.unit.test_recovery_durability import control as durable_control  # noqa: F401
from app.tests.unit.test_request_flow import intent
from app.tests.unit.test_tool_documentation_metadata import meaning
from app.tools.base import Tool
from app.tools.candidate_resolver import ToolCandidateResolver
from app.tools.registry import ExecutionBinding, ToolRegistry
from app.tools.search import ToolSearchService
from app.tools.search.index import ToolSearchIndex
from app.tools.web_search import WebSearchInput
from langgraph.graph import END, StateGraph
from pydantic import BaseModel
from sqlalchemy import select


class AnnualInput(BaseModel):
    year: int


class AnnualTool(Tool[AnnualInput]):
    version = "1"
    input_model = AnnualInput
    effect_type = "read_only"
    description = "查询立昂微2024年年报合并口径营业收入、归母净利润、经营活动现金流净额，金额单位元"

    def __init__(self, name, *, succeeds=False):
        self.name, self.succeeds, self.calls = name, succeeds, 0

    async def run(self, data):
        self.calls += 1
        return ToolCallResult(tool_call_id="remote", tool_name=self.name, trace_id="", status=(
            "success" if self.succeeds else "failed"), execution_state="not_started" if not self.succeeds else "completed",
            result=getattr(self, "result_payload", {"revenue": 12, "profit": -2, "cashflow": 3}) if self.succeeds else None,
            error=None if self.succeeds else ToolError(code=getattr(self, "error_code", "provider_request_rejected"), message="来源拒绝"))


class PublicTool(Tool[WebSearchInput]):
    name = "web_search"
    version = "1"
    input_model = WebSearchInput
    effect_type = "read_only"
    description = "联网检索公开财报原始披露"
    permission_scope: ClassVar[list[str]] = ["web:search"]

    def __init__(self):
        self.queries = []

    async def run(self, data):
        self.queries.append(data.query)
        return ToolCallResult(tool_call_id="remote", tool_name=self.name, trace_id="", status="success",
            result={"query": data.query, "answer": "收入12、归母净利润-2、经营现金流3（元）", "results": []})


class Models:
    def __init__(self):
        self.planning, self.selections, self.synthesis = [], [], []

    async def complete(self, **kwargs):
        payload = json.loads(kwargs["user_prompt"])
        assert "tool_candidates" not in payload, "来源失败不应进入重规划"
        self.synthesis.append(payload)
        answer = {"action": "answer", "answer": "收入12、归母净利润-2、经营现金流3（元）。"}
        return LlmCompletionResult(content=json.dumps(answer, ensure_ascii=False), model="controlled", provider="test")

    def bind_tools(self, tools, *, tool_choice):
        names = [tool["function"]["name"] for tool in tools]
        self.selections.append(names)
        return SimpleNamespace(invoke=lambda messages: plan_reply(tools[0], [
            {"id": "a", "name": "mcp_alpha_annual", "args": {"year": 2024}},
            {"id": "b", "name": "mcp_beta_annual", "args": {"year": 2024}},
            {"id": "w", "name": "web_search", "args": {
                "query": "立昂微 2024年年报 营业收入 归母净利润 经营活动现金流净额"}},
        ]))


@pytest.mark.parametrize("second_succeeds", [False, True])
@pytest.mark.parametrize("first_error", ["provider_request_rejected", "provider_permission_denied", "empty_success"])
def test_precomputed_sources_execute_without_replanning_and_keep_independent_receipts(request, second_succeeds, first_error):
    control = request.getfixturevalue("durable_control")
    sources = [AnnualTool("mcp.alpha.annual"), AnnualTool("mcp.alpha.other"),
               AnnualTool("mcp.beta.annual", succeeds=second_succeeds)]
    public, registry = PublicTool(), ToolRegistry()
    sources[0].error_code = first_error
    if first_error == "empty_success":
        sources[0].succeeds, sources[0].result_payload = True, []
    for tool in sources:
        tool.documentation = {"status": "partial", "output_fields": {
            "cashflow": meaning("经营活动现金流量净额")}}
        registry.register(tool, execution_binding=ExecutionBinding(provider="mcp", server_id=tool.name.split(".")[1],
            implementation_id=tool.name, deployment_revision="1"))
    registry.register(public)
    registry = registry.snapshot()
    service = ToolSearchService(registry=registry, index=ToolSearchIndex.build(
        specs=registry.specs(), catalog_digest=registry.catalog_digest), candidate_k=20)
    with control.session_factory() as db:
        run = db.get(AgentRun, control.run_id)
        assistant = db.scalar(select(AgentMessage).where(AgentMessage.run_id == run.run_id, AgentMessage.role == "assistant"))
        state = create_initial_state(session_id=run.session_id, session_key=run.session_key, tenant_id=run.tenant_id,
            user_id=run.user_id, request_id="r", assistant_message_id=assistant.message_id,
            user_message="请取得立昂微2024年年报的营业收入、归母净利润、经营活动现金流净额", trace_id=run.trace_id,
            tool_catalog_digest=registry.catalog_digest, permission_scopes=["web:search"])
    goal = "取得立昂微2024年年报合并口径的营业收入、归母净利润、经营活动现金流净额三项原始金额，保留单位和期间。" * 3
    state.update(request_flow_version="context-routing-v1", recovery_policy_version=RECOVERY_VERSION,
                 planner_iteration=1, intent=intent(goal=state["user_message"]),
                 plan={"tasks": [{"id": "annual", "task_goal": goal, "completion_criteria": ["三项金额完整，保留年报合并口径"]}]})
    original = deepcopy(state["plan"])
    models = Models()
    builder = StateGraph(AgentState)
    builder.add_node("task_scheduler", build_task_scheduler_node(registry))
    builder.add_node("tool_agent", build_tool_agent_node(registry_factory=lambda: registry, llm_client=models,
        candidate_resolver=ToolCandidateResolver(search_service=service)))
    builder.add_node("planner_agent", build_planner_agent_node(llm_client=models, tool_search_service=service))
    builder.add_node("synthesizer", build_synthesizer_node(llm_client=models))
    builder.add_node("verifier", lambda state: {})
    builder.set_entry_point("task_scheduler")
    builder.add_conditional_edges("task_scheduler", _dispatch_after_scheduler)
    builder.add_edge("tool_agent", "task_scheduler")
    builder.add_edge("planner_agent", "task_scheduler")
    builder.add_conditional_edges("synthesizer", _route_after_synthesis)
    builder.add_edge("verifier", END)
    with execution_control(control):
        result = builder.compile().invoke(state, {"recursion_limit": 50})
    assert [tool.calls for tool in sources] == [1, 0, 1]
    assert len(public.queries) == (0 if second_succeeds else 1)
    assert result["replan_request_count"] == 0
    assert result["source_attempt_queues"]["annual"]["status"] == "succeeded"
    assert result["draft_answer"] is not None
    assert result["plan"]["tasks"][0]["task_goal"] == original["tasks"][0]["task_goal"]
    if first_error == "empty_success":
        assert result["tool_calls"][0].result.status == "success"
        assert result["tool_calls"][0].result.result == []
        assert result["tool_calls"][0].result.error is None
        assert result["source_attempt_queues"]["annual"]["calls"][0]["last_error"]["code"] == "tool_result_empty"
    else:
        assert result["tool_calls"][0].result.error.code == first_error
    assert not result.get("planner_stop_reason")
    assert not result.get("pending_user_input") and not result.get("recovery_batches")
    assert len(models.selections) == 1
    if second_succeeds:
        material = models.synthesis[0]["research_materials"][0]
        assert material["field_semantics"]["output_fields"]["cashflow"]["description"] == "经营活动现金流量净额"
    assert len(result["source_attempt_queues"]["annual"]["calls"]) == 3
    if not second_succeeds:
        assert models.selections[0] == ["plan_source_calls"]
        assert result["source_attempt_queues"]["annual"]["calls"][-1]["request"]["tool_name"] == "web_search"
        assert len(public.queries[0]) < 128 < len(goal)
        assert all(word in public.queries[0] for word in ("立昂微", "2024", "营业收入", "归母净利润", "经营活动现金流净额"))
    with control.session_factory() as db:
        receipts = list(db.scalars(select(AgentRunToolReceipt).where(AgentRunToolReceipt.run_id == control.run_id)))
        assert len(receipts) == (2 if second_succeeds else 3)
        assert all(row.status == "settled" for row in receipts)
