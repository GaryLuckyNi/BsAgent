"""真实检索、图与执行器，仅在模型和远端工具边界使用替身。"""

from types import SimpleNamespace

import pytest
from app.graph import runtime
from app.schemas.tools import ToolCallResult
from app.skills.definitions import SkillCard
from app.tests.unit.test_planning_candidates import QUERY
from app.tests.unit.test_request_flow import Answers, intent
from app.tools.base import Tool
from app.tools.registry import ToolRegistry
from app.tools.search import ToolSearchService
from app.tools.search.index import ToolSearchIndex
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field


class StockInput(BaseModel):
    name: str = Field(description="名称")
    fields: list[str] = Field(description="可选字段 ts_code,name,list_date,delist_date,list_status")


class StockTool(Tool):
    name = "mcp.tushare.stock_basic"
    description = "获取基础信息数据，包括股票代码、名称、上市日期、退市日期等。"
    version = "1.0.0"
    input_model = StockInput

    def __init__(self, calls):
        self.calls = calls

    async def run(self, data):
        self.calls.append((self.name, data.model_dump()))
        return ToolCallResult(tool_call_id="provider", tool_name=self.name, status="success",
            result={"name": data.name, "ts_code": "example.SZ", "list_date": "示例日期",
                    "delist_date": None, "list_status": "L"}, trace_id="provider")


class Model(Answers):
    def __init__(self, route="plan", proposal=None):
        ref = "tool:mcp.tushare.stock_basic@1.0.0"
        initial = intent(goal=QUERY, route=route)
        if route == "direct_answer":
            initial["answer"] = "根据已有材料可解释公司基础资料。"
        super().__init__(initial, *([
            {"tasks": [{"id": "basic_info", "task_goal": QUERY, "tool_candidate_refs": [ref],
                        **({"proposed_tool_call": proposal} if proposal is not None else {})}]},
        ] if route == "plan" else []), {"action": "answer", "answer": "已取得公司基础资料，退市日期需结合上市状态解释。"})
        self.bindings = []

    def bind_tools(self, schemas, *, tool_choice):
        self.bindings.append(schemas)
        name = schemas[0]["function"]["name"]
        return SimpleNamespace(invoke=lambda messages: AIMessage(content="", tool_calls=[{
            "id": "provider-call", "name": name,
            "args": {"name": "银之杰", "fields": ["ts_code", "name", "list_date", "delist_date", "list_status"]},
        }]))


def graph_setup(monkeypatch):
    calls, searches = [], []
    registry = ToolRegistry()
    registry.register(StockTool(calls))
    snapshot = registry.snapshot()
    service = ToolSearchService(registry=snapshot, index=ToolSearchIndex.build(
        specs=snapshot.specs(), catalog_digest=snapshot.catalog_digest), candidate_k=20)
    search = service.search

    def recording_search(request, *, context):
        searches.append(request.query)
        return search(request, context=context)

    service.search = recording_search

    class Skills:
        def discover(self, **kwargs):
            return [SkillCard(name="company_snapshot", summary="查询基础资料、行情、公告的完整公司画像",
                              definition_hash="broad-skill")]

        def get_definition_layers(self, **kwargs):
            raise AssertionError("基础资料查询不应执行整套画像 Skill")

    monkeypatch.setattr(runtime, "build_default_skill_registry", Skills)
    return calls, searches, {"tool_registry_factory": lambda: snapshot, "tool_search_service": service}


def test_one_task_reuses_candidates_and_executes_actual_tool_agent(monkeypatch, business_intent_catalog):
    calls, searches, kwargs = graph_setup(monkeypatch)
    llm = Model()
    result = runtime.run_question_answer_graph(**kwargs, business_intent_catalog=business_intent_catalog,
        llm_client=llm, session_id="s", tenant_id="t", user_id="u", user_message=QUERY)

    assert len(result["plan"]["tasks"]) == 1
    assert result["skill_workflows"] == {}
    assert [name for name, _ in calls] == ["mcp.tushare.stock_basic"]
    assert "delist_date" in calls[0][1]["fields"]
    assert searches == [QUERY]
    assert len(llm.calls) == 3  # Router、Planner、Synth；工具选择另计一次 bind 调用。
    assert len(llm.bindings) == 1
    assert any(event.get("source") == "planning_reuse" for event in result["tool_discovery_events"].values())
    assert result["final_response"]["content"]


def test_planner_proposal_saves_execution_model_call(monkeypatch, business_intent_catalog):
    calls, searches, kwargs = graph_setup(monkeypatch)
    llm = Model(proposal={"candidate_ref": "tool:mcp.tushare.stock_basic@1.0.0",
                         "arguments": {"name": "银之杰", "fields": [
                             "ts_code", "name", "list_date", "delist_date", "list_status"]}})
    result = runtime.run_question_answer_graph(**kwargs, business_intent_catalog=business_intent_catalog,
        llm_client=llm, session_id="s", tenant_id="t", user_id="u", user_message=QUERY)
    assert searches == [QUERY]
    assert calls == [("mcp.tushare.stock_basic", {"name": "银之杰", "fields": [
        "ts_code", "name", "list_date", "delist_date", "list_status"]})]
    assert len(llm.calls) == 3 and llm.bindings == []
    assert any(event.get("proposal_status") == "accepted"
               for event in result["tool_discovery_events"].values())
    record = result["tool_calls"][0]
    assert record.request.call_id.startswith("call_") and record.result.status == "success"


def test_independent_tasks_can_each_supply_a_proposal(monkeypatch, business_intent_catalog):
    calls, searches, kwargs = graph_setup(monkeypatch)
    llm = Model()
    tasks = [{"id": task_id, "task_goal": QUERY.replace("银之杰", name),
              "tool_candidate_refs": ["tool:mcp.tushare.stock_basic@1.0.0"],
              "proposed_tool_call": {"candidate_ref": "tool:mcp.tushare.stock_basic@1.0.0",
                                     "arguments": {"name": name, "fields": ["name"]}}}
             for task_id, name in (("first", "银之杰"), ("second", "寒武纪"))]
    llm.answers = (llm.answers[0], {"tasks": tasks}, llm.answers[-1])
    result = runtime.run_question_answer_graph(**kwargs, business_intent_catalog=business_intent_catalog,
        llm_client=llm, session_id="s", tenant_id="t", user_id="u", user_message=QUERY + "，以及寒武纪的基础信息")
    assert len(calls) == 2 and {args["name"] for _, args in calls} == {"银之杰", "寒武纪"}
    assert len(searches) == 1 and llm.bindings == []
    assert len({record.request.call_id for record in result["tool_calls"]}) == 2


@pytest.mark.parametrize("route", ["direct_answer", "synthesize"])
def test_context_only_routes_do_not_recall_tools(monkeypatch, business_intent_catalog, route):
    calls, searches, kwargs = graph_setup(monkeypatch)
    result = runtime.run_question_answer_graph(**kwargs, business_intent_catalog=business_intent_catalog,
        llm_client=Model(route), session_id="s", tenant_id="t", user_id="u", user_message=QUERY)
    assert calls == searches == []
    assert result["planning_tool_candidates"] is None
    assert result["final_response"]["content"]
