"""首次规划按需补召回；只测试能力发现，不执行业务工具。"""

import json

import pytest
from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities, PlannerOutputError
from app.schemas.tools import ToolSpec
from app.tests.unit.test_request_flow import Answers
from app.tools.planning_candidates import project_card


def card(name, description):
    return project_card(ToolSpec(name=name, version="1", description=description, input_schema={}))


def arguments():
    return {"goal": "取得收入、利润与现金流原始金额", "intent_type": "financial_research",
            "domain": "a_share_equity_research", "subdomain": "general_research", "task_results": (),
            "remaining_iterations": 2, "capabilities": PlannerCapabilities()}


def test_plan_requests_missing_capabilities_before_returning_independent_tasks():
    income, flow = card("mcp.public.statement", "收入与归母净利润"), card("mcp.public.flows", "经营现金流")
    final = {"tasks": [{"id": "income", "task_goal": "取得收入和归母净利润", "tool_candidate_refs": [income.ref]},
                       {"id": "flows", "task_goal": "取得经营现金流", "tool_candidate_refs": [flow.ref]}]}
    llm = Answers({"action": "discover_tools", "queries": ["年度利润表原始金额", "年度经营现金流"]}, final)
    searches = []

    def discover(queries):
        searches.append(queries)
        return PlannerCapabilities(tools=(income, flow))

    result = LlmTaskPlanner(llm_client=llm).plan(**arguments(), capability_search=discover)
    assert len(llm.calls) == 2 and len(searches) == 1
    assert searches[0] == ("年度利润表原始金额", "年度经营现金流")
    assert [task.tool_candidate_refs for task in result.tasks] == [[income.ref], [flow.ref]]
    final_payload = json.loads(llm.calls[1]["user_prompt"])
    assert final_payload["capability_search"]["remaining_rounds"] == 0
    assert {item["ref"] for item in final_payload["tool_candidates"]} == {income.ref, flow.ref}


def test_complete_plan_does_not_add_an_assessment_model_call():
    llm = Answers({"tasks": [{"id": "facts", "task_goal": "取得全部原始金额"}]})
    searches = []
    LlmTaskPlanner(llm_client=llm).plan(**arguments(), capability_search=searches.append)
    assert len(llm.calls) == 1 and searches == []


def test_nested_discovery_envelope_never_publishes_its_draft_tasks():
    llm = Answers({"capability_search": {"action": "discover_tools", "queries": ["必要能力"]},
                   "coverage": [], "tasks": [{"id": "draft", "task_goal": "尚未确认的取证"}]},
                  {"tasks": [{"id": "confirmed", "task_goal": "补召回后的取证"}]})
    searches = []

    def discover(queries):
        searches.append(queries)
        return PlannerCapabilities()

    result = LlmTaskPlanner(llm_client=llm).plan(**arguments(), capability_search=discover)
    assert len(searches) == 1 and len(llm.calls) == 2
    assert [task.id for task in result.tasks] == ["confirmed"]


def test_declared_coverage_gap_routes_to_search_using_capability_query_not_task_goal():
    llm = Answers({"coverage": [{"required_fact": "年度收入原始金额", "status": "unknown",
                                "task_id": "draft", "candidate_ref": None, "output_meaning": None}],
                   "tasks": [{"id": "draft", "task_goal": "取得某公司的整份复杂研究结果",
                              "tool_search_query": "年度利润表原始金额"}]},
                  {"tasks": [{"id": "confirmed", "task_goal": "取得年度原始金额"}]})
    searches = []

    def discover(queries):
        searches.append(queries)
        return PlannerCapabilities()

    result = LlmTaskPlanner(llm_client=llm).plan(**arguments(), capability_search=discover)
    assert searches == [("年度利润表原始金额",)]
    assert result.tasks[0].id == "confirmed" and len(llm.calls) == 2


def test_available_capability_with_task_binding_is_not_existing_business_evidence():
    income = card("mcp.public.statement", "收入原始金额")
    response = {"coverage": [{"required_fact": "收入", "status": "available", "task_id": "facts",
                              "candidate_ref": income.ref, "output_meaning": "收入原始金额"}],
                "tasks": [{"id": "facts", "task_goal": "取得收入", "tool_candidate_refs": [income.ref]}]}
    kwargs = arguments()
    kwargs["capabilities"] = PlannerCapabilities(tools=(income,))
    assert LlmTaskPlanner(llm_client=Answers(response)).plan(**kwargs).tasks[0].id == "facts"


@pytest.mark.parametrize("bad", [{"coverage": None, "tasks": []}, {"coverage": [], "tasks": None},
    {"coverage": [], "tasks": [{"id": [], "task_goal": "取数"}]},
    {"coverage": [], "tasks": [{"id": "facts", "task_goal": "取数", "tool_candidate_refs": None}]},
    {"coverage": [], "tasks": [{"id": "facts", "task_goal": "取数", "tool_search_query": {}}]},
])
def test_malformed_coverage_uses_existing_format_repair(bad):
    llm = Answers(bad, {"tasks": [{"id": "facts", "task_goal": "取得必要事实"}]})
    result = LlmTaskPlanner(llm_client=llm).plan(**arguments(), capability_search=lambda queries: None)
    assert result.tasks[0].id == "facts" and len(llm.calls) == 2


def test_coverage_paraphrase_is_not_an_execution_contract_or_string_equality_gate():
    income = card("mcp.public.statement", "查询收入与利润的原始金额")
    kwargs = arguments()
    kwargs["capabilities"] = PlannerCapabilities(tools=(income,))
    llm = Answers({"coverage": [{"required_fact": "收入", "status": "covered", "task_id": "facts",
                                "candidate_ref": income.ref, "output_meaning": "收入原始金额"}],
                   "tasks": [{"id": "facts", "task_goal": "取得收入", "tool_candidate_refs": [income.ref]}]})
    assert LlmTaskPlanner(llm_client=llm).plan(**kwargs).tasks[0].id == "facts"
    assert len(llm.calls) == 1


def test_internal_coverage_map_is_validated_without_becoming_execution_state():
    income = card("mcp.public.statement", "收入原始金额")
    final = {"coverage": [{"required_fact": "收入原始金额", "status": "covered", "task_id": "facts",
                           "candidate_ref": income.ref, "output_meaning": "收入原始金额"}],
             "tasks": [{"id": "facts", "task_goal": "取得收入金额", "tool_candidate_refs": [income.ref]}]}
    kwargs = arguments()
    kwargs["capabilities"] = PlannerCapabilities(tools=(income,))
    llm = Answers(final)
    result = LlmTaskPlanner(llm_client=llm).plan(**kwargs, capability_search=lambda queries: None)
    assert len(llm.calls) == 1
    assert set(result.model_dump()) == {"tasks"}


def test_coverage_cannot_reference_another_tasks_candidate():
    income, flow = card("mcp.public.statement", "收入"), card("mcp.public.flow", "现金流")
    final = {"coverage": [{"required_fact": "收入", "status": "covered", "task_id": "flows",
                           "candidate_ref": income.ref, "output_meaning": "收入"}],
             "tasks": [{"id": "flows", "task_goal": "取得现金流", "tool_candidate_refs": [flow.ref]}]}
    kwargs = arguments()
    kwargs["capabilities"] = PlannerCapabilities(tools=(income, flow))
    with pytest.raises(PlannerOutputError):
        LlmTaskPlanner(llm_client=Answers(final)).plan(**kwargs, capability_search=lambda queries: None)


def test_final_plan_progress_stream_restarts_after_capability_response():
    from app.graph.planning.progress_stream import ProgressFieldStream
    from app.llm.schemas import LlmStreamDelta

    class StreamingAnswers(Answers):
        async def complete_streamed(self, *, on_delta, **kwargs):
            response = await self.complete(**kwargs)
            on_delta(LlmStreamDelta(kind="content", delta=response.content))
            return response

    llm = StreamingAnswers({"action": "discover_tools", "queries": ["必要能力"]},
                           {"tasks": [{"id": "facts", "task_goal": "补齐后取得必要事实"}]})
    published, failures = [], []
    progress = ProgressFieldStream(kind="plan", on_text=published.append, on_failed=lambda: failures.append(True))
    LlmTaskPlanner(llm_client=llm).plan(**arguments(), capability_search=lambda queries: PlannerCapabilities(),
                                      on_delta=progress)
    assert published[-1] == "1. 补齐后取得必要事实"
    assert failures == []


def test_second_discovery_request_is_repaired_without_executing_more_searches():
    request = {"action": "discover_tools", "queries": ["原始金额"]}
    llm = Answers(request, request, {"tasks": [{"id": "facts", "task_goal": "取得尚缺金额"}]})
    searches = []

    def discover(queries):
        searches.append(queries)
        return PlannerCapabilities()

    LlmTaskPlanner(llm_client=llm).plan(**arguments(), capability_search=discover)
    assert len(searches) == 1 and len(llm.calls) == 3


def test_discovery_cannot_authorize_undisclosed_tool_references():
    llm = Answers({"action": "discover_tools", "queries": ["原始金额"]},
                  {"tasks": [{"id": "facts", "task_goal": "取数", "tool_candidate_refs": ["tool:unknown@1"]}]})
    with pytest.raises(PlannerOutputError):
        LlmTaskPlanner(llm_client=llm).plan(**arguments(), capability_search=lambda queries: PlannerCapabilities())


@pytest.mark.parametrize("queries", [[], [" "], ["重复", " 重复 "], ["a", "b", "c", "d"], ["x" * 513],
                                     ["api_key=verysecretvalue123"]])
def test_invalid_capability_queries_never_reach_search(queries):
    llm = Answers({"action": "discover_tools", "queries": queries},
                  {"tasks": [{"id": "facts", "task_goal": "取数"}]})
    searches = []
    LlmTaskPlanner(llm_client=llm).plan(**arguments(), capability_search=searches.append)
    assert searches == []


def test_planner_bound_source_choice_is_not_overwritten_by_grouped_retrieval():
    from app.tests.unit.test_tool_candidate_resolver import FakeSearchService, _hit, _spec
    from app.tools.candidate_resolver import ToolCandidateResolver
    from app.tools.planning_candidates import PlanningToolSnapshot
    from app.tools.search import ToolSearchResponse

    chosen = _spec("mcp.tushare.statement")
    chosen.effect_type = "read_only"

    class Sources(FakeSearchService):
        def source_id(self, name):
            return "alternative" if name.startswith("mcp.other.") else "primary"

        def search_by_source(self, request, *, context, per_source):
            return ToolSearchResponse(catalog_digest="digest", mode="bm25", hits=[
                _hit("mcp.tushare.ratios"), _hit("mcp.other.statement")])

    service = Sources([_hit(chosen.name), _hit("mcp.tushare.ratios"), _hit("mcp.other.statement")])
    service.specs[chosen.name] = chosen
    entry = project_card(chosen)
    snapshot = PlanningToolSnapshot(request_id="r", catalog_digest="digest", query_fingerprint="q", candidates=[entry])
    result = ToolCandidateResolver(search_service=service).resolve(
        task_goal="取得原始金额", tenant_id="t", user_id="u", permission_scopes=("mcp:tushare", "web:search"),
        request_id="r", planning_snapshot=snapshot, candidate_refs=(entry.ref,), has_successful_dependencies=False)
    assert [spec.name for spec in result.candidates] == [chosen.name, "mcp.other.statement", "web_search"]
    assert result.multi_source


def test_node_saves_supplemented_snapshot_before_publishing_one_plan():
    from app.graph.nodes.planner_agent import build_planner_agent_node
    from app.graph.state import create_initial_state
    from app.tests.unit.test_request_flow import intent
    from app.tests.unit.test_tool_candidate_resolver import FakeSearchService, _hit
    from app.tools.search import ToolSearchResponse

    initial, added = "mcp.public.ratios", "mcp.public.statement"

    class Search(FakeSearchService):
        def source_id(self, name):
            return "public"

        def search(self, request, *, context):
            name = added if request.query == "利润表原始金额" else initial
            return ToolSearchResponse(catalog_digest="digest", mode="bm25", hits=[_hit(name)])

    value = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="取得年度收入与利润金额")
    value["intent"] = intent(goal=value["user_message"])
    llm = Answers({"action": "discover_tools", "queries": ["利润表原始金额"]},
                  {"tasks": [{"id": "amounts", "task_goal": "取得收入和利润金额",
                              "tool_candidate_refs": [f"tool:{added}@1.0.0"]}]})
    search = Search([_hit(initial), _hit(added)])
    build_planner_agent_node(llm_client=llm, tool_search_service=search)(value)
    assert value["planner_iteration"] == 1 and value["task_results"] == []
    assert value.get("planner_feedback_count", 0) == 0
    refs = {item["ref"] for item in value["planning_tool_candidates"]["candidates"]}
    assert value["plan"]["tasks"][0]["tool_candidate_refs"][0] in refs
    assert len(llm.calls) == 2


def test_supplemented_queries_share_card_budget_without_starving_later_queries():
    from app.tests.unit.test_planning_candidates import candidate_state
    from app.tests.unit.test_tool_candidate_resolver import FakeSearchService, _hit
    from app.tools.planning_candidates import recall_candidates
    from app.tools.search import ToolSearchResponse

    names = [f"mcp.public.{group}_{i}" for group in ("one", "two", "original") for i in range(8)]

    class Search(FakeSearchService):
        def source_id(self, name):
            return "public"

        def search(self, request, *, context):
            group = request.query if request.query in {"one", "two"} else "original"
            return ToolSearchResponse(catalog_digest="digest", mode="bm25", hits=[
                _hit(f"mcp.public.{group}_{i}") for i in range(8)])

    search = Search([_hit(name) for name in names])
    value = candidate_state("original user question")
    snapshot = recall_candidates(state=value, search_service=search, top_k=5, supplemental_queries=("one", "two"))
    assert len(snapshot.candidates) == 8
    assert {f"mcp.public.{group}_0" for group in ("one", "two", "original")} <= {
        item.tool_name for item in snapshot.candidates}


def test_supplement_failure_preserves_current_request_candidates():
    from app.tests.unit.test_planning_candidates import candidate_service, candidate_state
    from app.tools.planning_candidates import recall_candidates
    from app.tools.search.index import ToolSearchUnavailableError

    value = candidate_state()
    service = candidate_service()
    first = recall_candidates(state=value, search_service=service, top_k=5)
    value["planning_tool_candidates"] = first.model_dump(mode="json")

    def unavailable(*args, **kwargs):
        raise ToolSearchUnavailableError()

    service.search = unavailable
    result = recall_candidates(state=value, search_service=service, top_k=5, supplemental_queries=("补充能力",))
    assert result.reason == "tool_search_unavailable"
    assert result.candidates == first.candidates


def test_supplement_preserves_previously_disclosed_candidates():
    from app.tests.unit.test_planning_candidates import candidate_state
    from app.tests.unit.test_tool_candidate_resolver import FakeSearchService, _hit
    from app.tools.planning_candidates import recall_candidates
    from app.tools.search import ToolSearchResponse

    names = [f"mcp.public.{group}_{i}" for group in ("one", "two", "original") for i in range(8)]

    class Search(FakeSearchService):
        def source_id(self, name):
            return "public"

        def search(self, request, *, context):
            group = request.query if request.query in {"one", "two"} else "original"
            return ToolSearchResponse(catalog_digest="digest", mode="bm25", hits=[
                _hit(f"mcp.public.{group}_{i}") for i in range(request.top_k)])

    search = Search([_hit(name) for name in names])
    value = candidate_state("original user question")
    first = recall_candidates(state=value, search_service=search, top_k=5)
    value["planning_tool_candidates"] = first.model_dump(mode="json")
    result = recall_candidates(state=value, search_service=search, top_k=5, supplemental_queries=("one", "two"))
    assert {card.ref for card in first.candidates} <= {card.ref for card in result.candidates}
    assert len(result.candidates) == 8


def test_capability_query_market_constraint_is_kept_when_goal_has_no_market():
    from app.tests.unit.test_tool_search_service import _CatalogTool
    from app.tools.registry import ToolRegistry
    from app.tools.search import (
        ToolSearchContext,
        ToolSearchIndex,
        ToolSearchInput,
        ToolSearchService,
    )

    registry = ToolRegistry()
    registry.register(_CatalogTool(name="mcp.public.wrong", description="美股上市公司利润表查询"))
    registry.register(_CatalogTool(name="mcp.public.right", description="沪深上市公司利润表查询"))
    snapshot = registry.snapshot()
    service = ToolSearchService(registry=snapshot, index=ToolSearchIndex.build(
        specs=snapshot.specs(), catalog_digest=snapshot.catalog_digest), candidate_k=20)
    response = service.search(ToolSearchInput(query="mcp.public.wrong A股上市公司利润表", top_k=1),
        context=ToolSearchContext(tenant_id="t", user_id="u", task_goal="查询某公司年报"))
    assert [hit.tool_name for hit in response.hits] == ["mcp.public.right"]


def test_first_planning_recall_reserves_supplement_slots_even_with_top_k_eight(monkeypatch):
    from app.graph.nodes.planner_agent import _discover_capabilities
    from app.tests.unit.test_planning_candidates import candidate_state
    from app.tests.unit.test_tool_candidate_resolver import FakeSearchService, _hit
    from app.tools.search import ToolSearchResponse

    class Search(FakeSearchService):
        def source_id(self, name):
            return "public"

        def search(self, request, *, context):
            return ToolSearchResponse(catalog_digest="digest", mode="bm25", hits=self.hits[:request.top_k])

    service = Search([_hit(f"mcp.public.item_{i}") for i in range(8)])
    from app.core.config import settings

    monkeypatch.setattr(settings, "tool_search_top_k", 8)
    result = _discover_capabilities(state=candidate_state(), domain="domain", subdomain="subdomain",
                                   skill_registry=None, tool_search_service=service)
    assert len(result.tools) == 5
