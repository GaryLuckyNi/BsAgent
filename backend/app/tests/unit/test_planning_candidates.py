"""规划前能力披露与执行引用，不调用外部接口。"""

import json

import pytest
from app.graph.planning.contracts import SubTaskModel
from app.graph.state import create_initial_state
from app.schemas.tools import ToolSpec
from app.tests.unit.test_tool_search_service import _CatalogTool
from app.tools.registry import ToolRegistry
from app.tools.search import ToolSearchService
from app.tools.search.index import ToolSearchIndex, ToolSearchUnavailableError
from pydantic import ValidationError

QUERY = "获取银之杰基础信息数据，包括股票代码、名称、上市日期、退市日期等"


def candidate_state(query=QUERY):
    value = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message=query)
    value["intent"] = {"goal": query}
    return value


def stock_spec(**changes):
    return ToolSpec(**{
        "name": "mcp.tushare.stock_basic", "version": "1.0.0",
        "description": "获取基础信息数据，包括股票代码、名称、上市日期、退市日期等。",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "名称"},
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "可选字段：ts_code,name,list_date,delist_date,list_status"},
            "private_config": {"description": "不向模型披露的服务器注入字段"},
        }}, "server_injected_fields": ["private_config"], **changes,
    })


def candidate_service():
    registry = ToolRegistry()
    tool = _CatalogTool(name=stock_spec().name, description=stock_spec().description)
    registry.register(tool)
    snapshot = registry.snapshot()
    return ToolSearchService(registry=snapshot, index=ToolSearchIndex.build(
        specs=snapshot.specs(), catalog_digest=snapshot.catalog_digest), candidate_k=20)


def test_candidate_refs_are_optional_and_preserved():
    assert SubTaskModel(id="facts", task_goal="查询资料").tool_candidate_refs == []
    task = SubTaskModel(id="facts", task_goal="查询资料", tool_candidate_refs=["tool:a@1"])
    assert task.tool_candidate_refs == ["tool:a@1"]


@pytest.mark.parametrize("extra", [{"skill_name": "snapshot"}, {"execution_type": "analysis"}])
def test_candidate_refs_cannot_override_skill_or_analysis(extra):
    with pytest.raises(ValidationError, match="候选"):
        SubTaskModel(id="facts", task_goal="查询资料", tool_candidate_refs=["tool:a@1"], **extra)


@pytest.mark.parametrize("refs", [["tool:a@1"] * 2, [""], [" "]])
def test_candidate_refs_reject_duplicate_or_empty_values(refs):
    with pytest.raises(ValidationError):
        SubTaskModel(id="facts", task_goal="查询资料", tool_candidate_refs=refs)


def test_initial_state_has_no_planning_candidates():
    value = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="查询")
    assert value["planning_tool_candidates"] is None


def test_card_projects_field_hints_without_full_schema_or_injected_fields():
    from app.tools.planning_candidates import project_card

    card = project_card(stock_spec())
    assert card.ref == "tool:mcp.tushare.stock_basic@1.0.0"
    assert "delist_date" in card.schema_excerpt and "名称" in card.schema_excerpt
    assert "private_config" not in card.schema_excerpt
    assert "input_schema" not in card.model_dump()
    assert "output_schema" not in card.model_dump()


def test_projection_budgets_preserve_field_hints_and_redact_credentials():
    from app.tools.planning_candidates import project_card

    spec = stock_spec(description="用途" * 4000 + " Bearer abcdefghijklmnopqrstuvwxyz")
    card = project_card(spec)
    assert card.truncated
    assert len(card.description) + len(card.schema_excerpt) <= 1200
    assert "delist_date" in card.schema_excerpt
    assert "abcdefghijklmnopqrstuvwxyz" not in json.dumps(card.model_dump())


def test_snapshot_rejects_duplicate_refs():
    from app.tools.planning_candidates import PlanningToolSnapshot, project_card

    card = project_card(stock_spec())
    with pytest.raises(ValidationError):
        PlanningToolSnapshot(request_id="r", catalog_digest="d", query_fingerprint="q",
                             candidates=[card, card])


def test_original_query_recall_uses_real_bm25_once_and_caches(monkeypatch):
    from app.tools.planning_candidates import recall_candidates

    service, value, calls = candidate_service(), candidate_state(), []
    original = service.search

    def search(request, *, context):
        calls.append((request, context))
        return original(request, context=context)

    monkeypatch.setattr(service, "search", search)
    snapshot = recall_candidates(state=value, search_service=service, top_k=5)
    assert [card.tool_name for card in snapshot.candidates] == [stock_spec().name]
    assert [request.query for request, _ in calls] == [QUERY]
    value["planning_tool_candidates"] = snapshot.model_dump(mode="json")
    assert recall_candidates(state=value, search_service=service, top_k=5) == snapshot
    assert len(calls) == 1
    value["permission_scopes"] = ["changed"]
    recall_candidates(state=value, search_service=service, top_k=5)
    assert len(calls) == 2


@pytest.mark.parametrize("query", ["查一下这个", "背景材料" * 150 + QUERY])
def test_contextual_or_long_query_has_bounded_auxiliary_search(monkeypatch, query):
    from app.tools.planning_candidates import recall_candidates

    service, value, calls = candidate_service(), candidate_state(query), []
    value["intent"]["goal"] = QUERY
    original = service.search

    def search(request, *, context):
        calls.append(request.query)
        assert len(context.task_goal) <= 500
        return original(request, context=context)

    monkeypatch.setattr(service, "search", search)
    snapshot = recall_candidates(state=value, search_service=service, top_k=5)
    assert len(calls) == 2 and all(len(query) <= 512 for query in calls)
    assert any(card.tool_name == stock_spec().name for card in snapshot.candidates)
    assert snapshot.truncated == (len(query) > 512)


def test_missing_service_and_expected_search_failure_are_explicit(monkeypatch):
    from app.tools.planning_candidates import recall_candidates

    value = candidate_state()
    assert recall_candidates(state=value, search_service=None, top_k=5).reason == "tool_search_service_missing"
    service = candidate_service()

    def fail(*args, **kwargs):
        raise ToolSearchUnavailableError()

    monkeypatch.setattr(service, "search", fail)
    assert recall_candidates(state=value, search_service=service, top_k=5).reason == "tool_search_unavailable"

    def broken(*args, **kwargs):
        raise RuntimeError("programming error")

    monkeypatch.setattr(service, "search", broken)
    with pytest.raises(RuntimeError, match="programming error"):
        recall_candidates(state=value, search_service=service, top_k=5)


def test_recall_checks_run_catalog_identity():
    from app.tools.planning_candidates import recall_candidates
    from app.tools.registry import ToolCatalogChangedError

    value = candidate_state()
    value["tool_catalog_digest"] = "different"
    with pytest.raises(ToolCatalogChangedError):
        recall_candidates(state=value, search_service=candidate_service(), top_k=5)


def test_planner_receives_tool_cards_and_refs_in_one_model_call():
    from app.graph.nodes.planner_agent import build_planner_agent_node
    from app.tests.unit.test_request_flow import Answers, intent

    value, service = candidate_state(), candidate_service()
    value["intent"] = intent(goal=QUERY, available_information={"已知": "银之杰"})
    ref = "tool:mcp.tushare.stock_basic@1.0.0"
    llm = Answers({"tasks": [{"id": "basic_info", "task_goal": QUERY, "tool_candidate_refs": [ref]}]})
    result = build_planner_agent_node(llm_client=llm, tool_search_service=service)(value)
    payload = json.loads(llm.calls[0]["user_prompt"])
    assert len(llm.calls) == 1
    assert payload["tool_candidates"][0]["ref"] == ref
    assert "input_schema" not in json.dumps(payload)
    assert result["plan"]["tasks"][0]["tool_candidate_refs"] == [ref]
    assert result["planning_tool_candidates"]["catalog_digest"] == service.catalog_digest
    assert payload["planning_constraints"]["request_context"]["available_information"] == {"已知": "银之杰"}


def test_planner_rejects_undisclosed_candidate_even_after_repair():
    from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities, PlannerOutputError
    from app.tests.unit.test_request_flow import Answers

    llm = Answers({"tasks": [{"id": "facts", "task_goal": "取数", "tool_candidate_refs": ["tool:fake@1"]}]})
    with pytest.raises(PlannerOutputError):
        LlmTaskPlanner(llm_client=llm).plan(goal=QUERY, intent_type="financial_research",
            domain="a_share_equity_research", subdomain="general_research",
            capabilities=PlannerCapabilities(), task_results=(), remaining_iterations=2)
    assert len(llm.calls) == 2


def test_review_retains_completed_historical_refs_but_rejects_new_use():
    from app.graph.planning.contracts import PlanModel, TaskResultModel
    from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities, PlannerOutputError
    from app.tests.unit.test_request_flow import Answers

    current = PlanModel.model_validate({"tasks": [{
        "id": "done", "task_goal": "取得基础资料", "tool_candidate_refs": ["tool:old@1"],
    }]})
    completed = TaskResultModel(task_id="done", status="success", content="已有基础资料")
    kwargs = {"goal": QUERY, "user_message": QUERY, "current_plan": current, "task_results": (completed,),
              "capabilities": PlannerCapabilities(), "remaining_revisions": 1}
    tasks = [current.tasks[0].model_dump(), {"id": "news", "task_goal": "查询公告", "depends_on": ["done"]}]
    llm = Answers({"action": "revise", "reason": "需要公告", "plan": {"tasks": tasks}})
    decision = LlmTaskPlanner(llm_client=llm).review(**kwargs)
    assert decision.plan.tasks[0] == current.tasks[0]
    assert "tool_candidates" in json.loads(llm.calls[0]["user_prompt"])
    tasks[1]["tool_candidate_refs"] = ["tool:old@1"]
    bad = Answers({"action": "revise", "reason": "借用引用", "plan": {"tasks": tasks}})
    with pytest.raises(PlannerOutputError):
        LlmTaskPlanner(llm_client=bad).review(**kwargs)


def test_scheduler_trace_projects_refs_not_card_body():
    from app.operations.langfuse_scheduler import scheduler_trace_input

    value = candidate_state()
    value["plan"] = {"tasks": [{"id": "basic_info", "task_goal": QUERY, "tool_candidate_refs": ["tool:a@1"]}]}
    projection = scheduler_trace_input(value)
    assert projection["plan"]["tasks"][0]["tool_candidate_refs"] == ["tool:a@1"]
    assert "schema_excerpt" not in json.dumps(projection)


def test_candidate_hint_change_does_not_allow_renaming_failed_work():
    from app.graph.planning.contracts import PlanModel, TaskResultModel
    from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities, PlannerOutputError
    from app.tests.unit.test_request_flow import Answers
    from app.tools.planning_candidates import project_card

    cards = tuple(project_card(stock_spec(name=f"mcp.provider.{name}")) for name in ("first", "second"))
    current = PlanModel.model_validate({"tasks": [{"id": "failed", "task_goal": QUERY,
        "tool_candidate_refs": [cards[0].ref]}]})
    llm = Answers({"action": "revise", "reason": "只换候选与ID", "plan": {"tasks": [{
        "id": "renamed", "task_goal": QUERY, "tool_candidate_refs": [cards[1].ref],
    }]}})
    with pytest.raises(PlannerOutputError):
        LlmTaskPlanner(llm_client=llm).review(goal=QUERY, user_message=QUERY, current_plan=current,
            task_results=(TaskResultModel(task_id="failed", status="terminal_failure", content="失败"),),
            capabilities=PlannerCapabilities(tools=cards), remaining_revisions=1, replan_request={"reason_type": "missing_task"})


def test_planning_prompt_does_not_equate_null_fields_with_business_status():
    from app.graph.nodes.planner_agent import build_planner_agent_node
    from app.tests.unit.test_request_flow import Answers, intent

    value = candidate_state()
    value["intent"] = intent(goal=QUERY)
    llm = Answers({"tasks": [{"id": "facts", "task_goal": QUERY}]})
    build_planner_agent_node(llm_client=llm)(value)
    assert "字段空值不等于业务状态" in llm.calls[0]["system_prompt"]
    assert "Skill 和 analysis 任务的 tool_candidate_refs 必须为空" in llm.calls[0]["system_prompt"]
    assert "不得把已有匹配 Skill 重新展开成普通工具任务" in llm.calls[0]["system_prompt"]


@pytest.mark.parametrize("prompt_name", ["_SYSTEM_PROMPT", "_FEEDBACK_PROMPT"])
def test_skill_selection_depends_on_goal_coverage_not_explicit_workflow_request(prompt_name):
    """约束提示词契约；真实模型是否选中 Skill 另用自然提问实测。"""
    from app.graph.planning import planner

    prompt = getattr(planner, prompt_name)
    assert "无需用户明确要求使用 Skill 或按已有业务流程执行" in prompt
    assert "适用场景与执行范围覆盖用户目标，且不引入无关工作时，优先选择该 Skill" in prompt
    assert "不得把已有匹配 Skill 重新展开成普通工具任务" in prompt
    assert "仅主题相关、范围不匹配或会引入无关步骤时，不使用该 Skill" in prompt
    assert "Skill 已覆盖整个目标时，只创建一个 Skill 任务" in prompt
    assert "除非用户明确要求或存在具体未覆盖的必要目标，否则不追加核验、补充或交叉验证任务" in prompt
    assert "用户要求按已有业务流程执行，且" not in prompt


def test_known_redundant_candidate_refs_on_selected_skill_do_not_cost_a_repair():
    from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities, PlannerOutputError
    from app.skills.definitions import SkillCard
    from app.tests.unit.test_request_flow import Answers
    from app.tools.planning_candidates import project_card

    card = project_card(stock_spec())
    skill = SkillCard(name="workflow", summary="业务流程", definition_hash="hash")
    payload = {"tasks": [{"id": "flow", "task_goal": QUERY, "skill_name": "workflow",
                          "tool_candidate_refs": [card.ref]}]}
    kwargs = {"goal": QUERY, "intent_type": "financial_research", "domain": "a_share_equity_research",
              "subdomain": "general_research", "capabilities": PlannerCapabilities(skills=(skill,), tools=(card,)),
              "task_results": (), "remaining_iterations": 2}
    llm = Answers(payload)
    result = LlmTaskPlanner(llm_client=llm).plan(**kwargs)
    assert len(llm.calls) == 1
    assert result.tasks[0].skill_name == "workflow" and result.tasks[0].tool_candidate_refs == []
    payload["tasks"][0]["tool_candidate_refs"] = ["tool:unknown@1"]
    with pytest.raises(PlannerOutputError):
        LlmTaskPlanner(llm_client=Answers(payload)).plan(**kwargs)


def test_empty_candidate_refs_preserve_legacy_observation_signature():
    from hashlib import sha256

    from app.graph.planning.contracts import PlanModel
    from app.graph.planning.policy import observation_signature

    plan = PlanModel.model_validate({"tasks": [{"id": "facts", "task_goal": "取数"}]})
    legacy = plan.model_dump()
    legacy["tasks"][0].pop("tool_candidate_refs")
    expected = sha256(json.dumps({"plan": legacy, "results": []}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert observation_signature(plan, []) == expected


@pytest.mark.parametrize("snapshot", [None, {"id": "facts", "task_goal": "不同目标"},
    {"id": "facts", "task_goal": "取数", "tool_candidate_refs": ["tool:other@1"]},
    {"id": "facts", "task_goal": "取数", "source_requirement": "original_data"},
    {"id": "facts", "task_goal": "取数", "unexpected": True},
])
def test_legacy_snapshot_comparison_does_not_allow_real_task_changes(snapshot):
    from app.graph.planning.contracts import task_snapshot_matches

    assert not task_snapshot_matches(snapshot, SubTaskModel(id="facts", task_goal="取数"))
