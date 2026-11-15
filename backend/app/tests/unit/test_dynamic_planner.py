import json

import pytest
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.planning.contracts import PlanModel, TaskResultModel
from app.graph.planning.planner import (
    LlmTaskPlanner,
    PlannerCapabilities,
    UndisclosedPlannerCapabilityError,
)
from app.graph.planning.policy import PlannerMode
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult
from app.skills.definitions import SkillCard
from pydantic import ValidationError


class RecordingLlmClient:
    def __init__(self, payload: dict) -> None:
        self._content = json.dumps(payload, ensure_ascii=False)
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(
            content=self._content,
            model="fake-planner",
            provider="fake",
            usage={"total_tokens": 20},
        )


def test_plan_reads_legacy_summary_without_retaining_it_or_accepting_other_extras():
    payload = {"public_summary": "旧检查点的摘要", "tasks": [{"id": "facts", "task_goal": "核对事实"}]}
    parsed = PlanModel.model_validate(payload)
    assert "public_summary" not in parsed.model_dump()
    assert "public_summary" not in PlanModel.model_json_schema()["properties"]
    assert payload["public_summary"] == "旧检查点的摘要"
    with pytest.raises(ValidationError):
        PlanModel.model_validate({**payload, "unexpected": "仍然禁止"})


@pytest.mark.parametrize("review", [False, True])
def test_planner_does_not_request_a_separate_display_summary(review):
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="按公式推演并核对")
    state["intent"] = {"intent_type": "financial_research", "domain": "equity_research",
                       "subdomain": "company_research", "goal": state["user_message"]}
    old_plan = {"public_summary": "旧摘要", "tasks": [{"id": "facts", "task_goal": "取公式必要输入"}]}
    response = old_plan
    if review:
        state.update(plan=old_plan, planner_iteration=1)
        response = {"action": "finish", "reason": "已有材料足够", "unresolved_questions": []}
    llm = RecordingLlmClient(response)
    build_planner_agent_node(llm_client=llm)(state)
    assert len(llm.calls) == 1
    payload = json.loads(llm.calls[0]["user_prompt"])
    assert "public_summary" not in json.dumps(payload, ensure_ascii=False)
    assert "public_summary" not in llm.calls[0]["system_prompt"]
    assert "必要自变量" in llm.calls[0]["system_prompt"]
    if not review:
        assert "public_summary" not in state["plan"]


@pytest.mark.parametrize("source_constraint", ["unrestricted", "original_only", "confirmation_required", "unclear", None])
@pytest.mark.parametrize("review", [False, True])
def test_planner_does_not_inherit_retired_source_gate(source_constraint, review):
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="取得研究所需事实")
    state["intent"] = {"intent_type": "financial_research", "domain": "equity_research",
                       "subdomain": "company_research", "goal": "取得研究所需事实"}
    if source_constraint is not None:
        state["intent"]["source_constraint"] = source_constraint
    plan = {"tasks": [{"id": "facts", "task_goal": "取得研究所需事实"}]}
    response = plan
    if review:
        state["plan"] = plan
        state["planner_iteration"] = 1
        response = {"action": "finish", "reason": "按已有资料收尾", "unresolved_questions": []}
    client = RecordingLlmClient(response)
    build_planner_agent_node(llm_client=client)(state)
    payload = json.loads(client.calls[0]["user_prompt"])
    assert "source_constraint" not in payload["planning_constraints"]


def _skill(name: str) -> SkillCard:
    return SkillCard(
        name=name,
        summary=f"{name} 工作流",
        domains=["equity_research"],
        subdomains=["company_research"],
        definition_hash=f"hash:{name}",
    )


@pytest.mark.parametrize("requirement", ["public_information", "original_data", "unclear"])
def test_task_source_requirement_is_preserved_without_granting_permission(requirement):
    task = PlanModel.model_validate({"tasks": [{
        "id": "facts", "task_goal": "获取研究证据", "source_requirement": requirement,
    }]}).tasks[0]
    assert task.source_requirement == requirement


def test_missing_task_source_requirement_is_unclear_and_cannot_declare_allowed():
    assert PlanModel.model_validate({"tasks": [{"id": "facts", "task_goal": "取证"}]}).tasks[0].source_requirement == "unclear"
    with pytest.raises(ValidationError):
        PlanModel.model_validate({"tasks": [{
            "id": "facts", "task_goal": "取证", "source_requirement": "allowed",
        }]})



def test_plan_allows_reasoning_task_without_capability():
    plan = PlanModel.model_validate(
        {"tasks": [{"id": "judge", "task_goal": "归纳已取得证据", "depends_on": []}]}
    )

    assert plan.tasks[0].skill_name is None
    assert not hasattr(plan.tasks[0], "tool_name")


def test_plan_preserves_capability_oriented_tool_search_query():
    plan = PlanModel.model_validate(
        {
            "tasks": [
                {
                    "id": "company_news",
                    "task_goal": "查询银之杰（300085.SZ）的近期资讯",
                    "tool_search_query": (
                        "上市公司公告 监管披露 公告标题 PDF下载 财经新闻 新闻快讯 媒体报道"
                    ),
                }
            ]
        }
    )

    assert plan.tasks[0].tool_search_query == (
        "上市公司公告 监管披露 公告标题 PDF下载 财经新闻 新闻快讯 媒体报道"
    )


def test_plan_rejects_task_with_legacy_tool_binding():
    with pytest.raises(ValidationError):
        PlanModel.model_validate(
            {
                "tasks": [
                    {
                        "id": "bad",
                        "task_goal": "错误任务",
                        "depends_on": [],
                        "skill_name": "equity_research",
                        "tool_name": "mcp.stock.daily",
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    "tasks",
    [
        [
            {"id": "same", "task_goal": "任务一"},
            {"id": "same", "task_goal": "任务二"},
        ],
        [{"id": "unknown", "task_goal": "任务", "depends_on": ["missing"]}],
        [{"id": "self_ref", "task_goal": "任务", "depends_on": ["self_ref"]}],
        [
            {"id": "first", "task_goal": "任务一", "depends_on": ["second"]},
            {"id": "second", "task_goal": "任务二", "depends_on": ["first"]},
        ],
    ],
    ids=["duplicate", "unknown_dependency", "self_dependency", "cycle"],
)
def test_plan_rejects_invalid_task_graph(tasks):
    with pytest.raises(ValidationError):
        PlanModel.model_validate({"tasks": tasks})


def test_plan_rejects_more_than_twelve_tasks():
    with pytest.raises(ValidationError):
        PlanModel.model_validate(
            {
                "tasks": [
                    {"id": f"task_{index}", "task_goal": f"任务 {index}"} for index in range(13)
                ]
            }
        )


def test_llm_planner_only_discloses_skills_and_atomic_task_schema():
    llm = RecordingLlmClient(
        {
            "tasks": [
                {"id": "collect_market", "task_goal": "取得目标公司的最新行情"},
                {
                    "id": "analyze",
                    "task_goal": "结合行情判断趋势",
                    "depends_on": ["collect_market"],
                },
            ]
        }
    )
    planner = LlmTaskPlanner(llm_client=llm)

    plan = planner.plan(
        goal="分析海能达近期表现",
        intent_type="financial_research",
        domain="equity_research",
        subdomain="company_research",
        capabilities=PlannerCapabilities(skills=(_skill("company_snapshot"),)),
        task_results=(
            TaskResultModel(
                task_id="previous",
                status="business_failure",
                content="证券代码尚未解析",
                error_code="entity_not_found",
                tool_name="mcp.tushare.stock.daily",
            ),
        ),
        remaining_iterations=9,
        mode=PlannerMode(thinking=False, max_tokens=1024),
    )

    assert [task.id for task in plan.tasks] == ["collect_market", "analyze"]
    prompt = json.loads(llm.calls[0]["user_prompt"])
    assert prompt["skills"] == [
        {"name": "company_snapshot", "summary": "company_snapshot 工作流"}
    ]
    assert "tools" not in prompt
    assert "tool_name" not in prompt["required_schema"]["tasks"][0]
    assert "tool_search_query" in prompt["required_schema"]["tasks"][0]
    assert "source_requirement" in prompt["required_schema"]["tasks"][0]
    assert len(llm.calls) == 1
    assert prompt["observations"][0]["error_code"] == "entity_not_found"
    assert prompt["timezone"] == "Asia/Shanghai"
    assert prompt["current_datetime"].startswith("20")
    assert "草案只是执行建议，不授予权限" in llm.calls[0]["system_prompt"]
    assert "proposed_tool_call" in prompt["required_schema"]["tasks"][0]
    assert llm.calls[0]["thinking"] is False
    assert llm.calls[0]["max_tokens"] == 1024


def test_llm_planner_retries_once_without_thinking_after_length_truncation():
    class TruncatedThenValidLlm:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return LlmCompletionResult(
                    content="",
                    model="fake-planner",
                    provider="fake",
                    finish_reason="length",
                )
            return LlmCompletionResult(
                content=json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "collect_evidence",
                                "task_goal": "获取市场证据",
                            },
                            {
                                "id": "analyze_evidence",
                                "task_goal": "分析市场证据",
                                "depends_on": ["collect_evidence"],
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                model="fake-planner",
                provider="fake",
                finish_reason="stop",
            )

    llm = TruncatedThenValidLlm()

    plan = LlmTaskPlanner(llm_client=llm).plan(
        goal="分析大金融板块是否值得投资",
        intent_type="financial_research",
        domain="a_share_equity_research",
        subdomain="general_research",
        capabilities=PlannerCapabilities(),
        task_results=(),
        remaining_iterations=10,
    )

    assert [task.id for task in plan.tasks] == ["collect_evidence", "analyze_evidence"]
    assert len(llm.calls) == 2
    assert llm.calls[0]["thinking"] is False
    assert llm.calls[0]["max_tokens"] == 2048
    assert llm.calls[1]["thinking"] is False
    assert "上一次输出被截断" in llm.calls[1]["user_prompt"]


def test_llm_planner_rejects_undisclosed_skill():
    llm = RecordingLlmClient(
        {"tasks": [{"id": "hidden", "task_goal": "执行任务", "skill_name": "hidden_skill"}]}
    )
    planner = LlmTaskPlanner(llm_client=llm)

    with pytest.raises(UndisclosedPlannerCapabilityError):
        planner.plan(
            goal="分析公司",
            intent_type="financial_research",
            domain="equity_research",
            subdomain="company_research",
            capabilities=PlannerCapabilities(skills=(_skill("visible_skill"),)),
            task_results=(),
            remaining_iterations=10,
        )


def test_planner_node_discovers_skills_and_tools_without_binding_task():
    class Registry:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def discover(self, **kwargs):
            self.calls.append(kwargs)
            return [_skill("company_snapshot")]

    class ToolSearch:
        catalog_digest = "digest"

        def search(self, request, *, context):
            from app.tools.search import ToolSearchResponse
            return ToolSearchResponse(catalog_digest="digest", mode="bm25", hits=[])

        def resolve_specs(self, names):
            assert names == []
            return []

    registry = Registry()
    llm = RecordingLlmClient(
        {"tasks": [{"id": "market", "task_goal": "查询海能达近期行情"}]}
    )
    state = create_initial_state(
        session_id="planner-node",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析一下海能达近期表现",
    )
    state["intent"] = {
        "intent_type": "financial_research",
        "domain": "equity_research",
        "subdomain": "company_research",
        "goal": "分析海能达近期表现",
        "confidence": 0.95,
    }

    result = build_planner_agent_node(
        llm_client=llm,
        skill_registry=registry,
        tool_search_service=ToolSearch(),
    )(state)

    assert registry.calls == [{"domain": "equity_research", "subdomain": "company_research"}]
    assert result["plan"] == {
        "tasks": [
            {
                    "id": "market",
                    "task_goal": "查询海能达近期行情",
                    "tool_search_query": None,
                    "tool_candidate_refs": [],
                    "proposed_tool_call": None,
                    "execution_type": "tool",
                    "completion_criteria": [],
                    "evidence_task_ids": [],
                    "depends_on": [],
                "skill_name": None,
                "source_requirement": "unclear",
            }
        ]
    }
    assert result["planner_iteration"] == 1
    planner_payload = json.loads(llm.calls[0]["user_prompt"])
    assert "tools" not in planner_payload


def test_planner_discloses_all_scoped_skills_in_stable_order():
    class Registry:
        def discover(self, **kwargs):
            assert kwargs == {
                "domain": "equity_research",
                "subdomain": "company_research",
            }
            return [_skill("zeta_skill"), _skill("alpha_skill")]

    llm = RecordingLlmClient(
        {"tasks": [{"id": "analyze", "task_goal": "分析公司经营情况"}]}
    )
    state = create_initial_state(
        session_id="planner-skill-disclosure",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析公司经营情况",
    )
    state["intent"] = {
        "intent_type": "financial_research",
        "domain": "equity_research",
        "subdomain": "company_research",
        "goal": "分析公司经营情况",
        "confidence": 0.95,
    }

    build_planner_agent_node(
        llm_client=llm,
        skill_registry=Registry(),
    )(state)

    planner_payload = json.loads(llm.calls[0]["user_prompt"])
    assert [card["name"] for card in planner_payload["skills"]] == [
        "alpha_skill",
        "zeta_skill",
    ]


def test_planner_node_does_not_stream_private_planning_reasoning():
    class NativeStreamingLlm(RecordingLlmClient):
        def __init__(self) -> None:
            super().__init__({"tasks": [{"id": "analyze", "task_goal": "分析市场下跌原因"}]})
            self.used_native_stream = False

        async def complete_streamed(self, *, on_delta, **kwargs):
            self.used_native_stream = True
            return await self.complete(**kwargs)



    llm = NativeStreamingLlm()
    state = create_initial_state(
        session_id="planner-node",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="为什么今天 A 股跌得这么多？",
    )
    state["intent"] = {
        "intent_type": "financial_research",
        "domain": "a_share_equity_research",
        "subdomain": "general_research",
        "goal": "分析当日 A 股市场下跌的可能原因",
        "confidence": 0.95,
    }

    build_planner_agent_node(llm_client=llm)(state)

    assert llm.used_native_stream is False


def test_namespace_preserves_discovery_fields():
    from app.graph.nodes.planner_agent import _namespace_replanned_tasks
    plan = PlanModel(tasks=[{"id": "facts", "task_goal": "查询公告", "tool_search_query": "监管披露"}])
    revised = _namespace_replanned_tasks(plan, revision=1)
    assert revised.tasks[0].tool_search_query == "监管披露"


def test_planner_repairs_undisclosed_skill_once():
    class Client(RecordingLlmClient):
        async def complete(self, **kwargs):
            result = await super().complete(**kwargs)
            if len(self.calls) == 1:
                return result
            return result.model_copy(update={"content": json.dumps({"tasks": [{"id": "facts", "task_goal": "核实已有资料"}]})})
    llm = Client({"tasks": [{"id": "facts", "task_goal": "核实资料", "skill_name": "unknown"}]})
    plan = LlmTaskPlanner(llm_client=llm).plan(goal="分析", intent_type="research", domain="equity", subdomain="company", capabilities=PlannerCapabilities(), task_results=(), remaining_iterations=1)
    assert plan.tasks[0].skill_name is None
    assert len(llm.calls) == 2
