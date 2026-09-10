import json

import pytest
from pydantic import ValidationError

from app.graph.planning.contracts import PlanModel, TaskResultModel
from app.graph.planning.planner import (
    LlmTaskPlanner,
    PlannerCapabilities,
    UndisclosedPlannerCapabilityError,
)
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult
from app.schemas.tools import ToolSpec
from app.skills.definitions import SkillCard
from app.tools.search import (
    ToolSearchHit,
    ToolSearchResponse,
)


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


def _skill(name: str) -> SkillCard:
    return SkillCard(
        name=name,
        summary=f"{name} 工作流",
        domains=["equity_research"],
        subdomains=["company_research"],
        definition_hash=f"hash:{name}",
    )


def _tool(name: str) -> ToolSearchHit:
    return ToolSearchHit(
        tool_name=name,
        tool_version="1.0.0",
        description=f"{name} 工具",
        required_arguments=["symbol"],
        rank=1,
        sources=["bm25"],
    )


def test_plan_allows_reasoning_task_without_capability():
    plan = PlanModel.model_validate(
        {
            "tasks": [
                {"id": "judge", "task_goal": "归纳已取得证据", "depends_on": []}
            ]
        }
    )

    assert plan.tasks[0].skill_name is None
    assert plan.tasks[0].tool_name is None


def test_plan_rejects_task_with_skill_and_tool():
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
                    {"id": f"task_{index}", "task_goal": f"任务 {index}"}
                    for index in range(13)
                ]
            }
        )


def test_llm_planner_only_discloses_lightweight_top_k_candidates():
    llm = RecordingLlmClient(
        {
            "tasks": [
                {
                    "id": "collect_market",
                    "task_goal": "取得目标公司的最新行情",
                    "tool_name": "mcp.tushare.stock.daily",
                },
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
        capabilities=PlannerCapabilities(
            skills=(_skill("company_snapshot"),),
            tools=(_tool("mcp.tushare.stock.daily"),),
        ),
        task_results=(
            TaskResultModel(
                task_id="previous",
                status="business_failure",
                content="证券代码尚未解析",
                error_code="entity_not_found",
            ),
        ),
        remaining_iterations=9,
    )

    assert [task.id for task in plan.tasks] == ["collect_market", "analyze"]
    prompt = json.loads(llm.calls[0]["user_prompt"])
    assert prompt["skills"] == [
        {
            "name": "company_snapshot",
            "summary": "company_snapshot 工作流",
        }
    ]
    assert prompt["tools"][0]["name"] == "mcp.tushare.stock.daily"
    assert "input_schema" not in prompt["tools"][0]
    assert prompt["observations"][0]["error_code"] == "entity_not_found"


@pytest.mark.parametrize(
    ("field", "hidden_name"),
    [
        ("skill_name", "hidden_skill"),
        ("tool_name", "mcp.hidden.tool"),
    ],
)
def test_llm_planner_rejects_undisclosed_capability(field, hidden_name):
    llm = RecordingLlmClient(
        {"tasks": [{"id": "hidden", "task_goal": "执行任务", field: hidden_name}]}
    )
    planner = LlmTaskPlanner(llm_client=llm)

    with pytest.raises(UndisclosedPlannerCapabilityError):
        planner.plan(
            goal="分析公司",
            intent_type="financial_research",
            domain="equity_research",
            subdomain="company_research",
            capabilities=PlannerCapabilities(
                skills=(_skill("visible_skill"),),
                tools=(_tool("mcp.visible.tool"),),
            ),
            task_results=(),
            remaining_iterations=10,
        )


def test_planner_node_discovers_capabilities_from_goal_and_stores_task_dag():
    daily = _tool("mcp.tushare.stock.daily")

    class Registry:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def discover(self, **kwargs):
            self.calls.append(kwargs)
            return [_skill("company_snapshot")]

    class ToolSearch:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def search(self, request, *, context):
            self.queries.append(request.query)
            return ToolSearchResponse(
                catalog_digest="digest",
                mode="hybrid",
                hits=[daily],
            )

        def resolve_specs(self, tool_names):
            if tool_names == ["search_tool"]:
                return [
                    ToolSpec(
                        name="search_tool",
                        version="1.0.0",
                        description="继续检索可用工具",
                        input_schema={
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    )
                ]
            raise AssertionError(tool_names)

    registry = Registry()
    tool_search = ToolSearch()
    llm = RecordingLlmClient(
        {
            "tasks": [
                {
                    "id": "market",
                    "task_goal": "查询海能达近期行情",
                    "tool_name": daily.tool_name,
                }
            ]
        }
    )
    state = create_initial_state(
        session_id="planner-node",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析一下海能达近期表现",
        permission_scopes=["mcp:market"],
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
        tool_search_service=tool_search,
    )(state)

    assert registry.calls == [
        {"domain": "equity_research", "subdomain": "company_research"}
    ]
    assert tool_search.queries == ["分析海能达近期表现"]
    assert result["plan"] == {
        "tasks": [
            {
                "id": "market",
                "task_goal": "查询海能达近期行情",
                "depends_on": [],
                "skill_name": None,
                "tool_name": daily.tool_name,
            }
        ]
    }
    assert result["planner_iteration"] == 1
    assert result["disclosed_tool_names"] == [daily.tool_name, "search_tool"]
