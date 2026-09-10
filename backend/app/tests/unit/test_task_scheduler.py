from app.graph.planning.contracts import PlanModel, TaskResultModel
from app.graph.planning.scheduler import plan_status, ready_tasks, recovery_action
from app.graph.nodes.skill_agent import build_skill_agent_node
from app.graph.runtime import _dispatch_after_scheduler, task_scheduler_node
from app.graph.state import AgentState, create_initial_state, merge_task_results
from app.llm.schemas import LlmCompletionResult
from app.skills.definitions import SkillDefinition, SkillExecutionGuide
from langgraph.graph import END, StateGraph


def _plan() -> PlanModel:
    return PlanModel.model_validate(
        {
            "tasks": [
                {"id": "market", "task_goal": "查询行情", "tool_name": "daily"},
                {
                    "id": "financial",
                    "task_goal": "查询财务",
                    "tool_name": "finance",
                },
                {
                    "id": "judge",
                    "task_goal": "综合判断",
                    "depends_on": ["market", "financial"],
                },
            ]
        }
    )


def test_scheduler_returns_all_independent_ready_tasks():
    ready = ready_tasks(_plan(), results=[])

    assert [task.id for task in ready] == ["market", "financial"]


def test_scheduler_returns_dependent_task_after_all_dependencies_succeed():
    ready = ready_tasks(
        _plan(),
        results=[
            TaskResultModel(task_id="market", status="success", content="行情"),
            TaskResultModel(task_id="financial", status="success", content="财务"),
        ],
    )

    assert [task.id for task in ready] == ["judge"]


def test_failed_dependency_blocks_downstream_task_and_requests_replan():
    results = [
        TaskResultModel(
            task_id="market",
            status="business_failure",
            content="证券代码未命中",
        )
    ]

    assert ready_tasks(_plan(), results=results) == []
    assert plan_status(_plan(), results=results) == "needs_replan"


def test_results_from_previous_plan_do_not_block_replanned_tasks():
    previous_failure = TaskResultModel(
        task_id="old_market",
        status="business_failure",
        content="旧计划失败",
    )

    assert [
        task.id for task in ready_tasks(_plan(), results=[previous_failure])
    ] == ["market", "financial"]
    assert plan_status(_plan(), results=[previous_failure]) == "running"


def test_terminal_failure_stops_plan():
    results = [
        TaskResultModel(
            task_id="market",
            status="terminal_failure",
            content="权限不足",
        )
    ]

    assert plan_status(_plan(), results=results) == "terminal_failure"


def test_task_result_reducer_replaces_same_task_and_preserves_other_branches():
    merged = merge_task_results(
        [
            {"task_id": "market", "status": "business_failure", "content": "失败"},
            {"task_id": "financial", "status": "success", "content": "财务"},
        ],
        [{"task_id": "market", "status": "success", "content": "行情"}],
    )

    assert merged == [
        {"task_id": "market", "status": "success", "content": "行情"},
        {"task_id": "financial", "status": "success", "content": "财务"},
    ]


def test_recovery_action_uses_typed_failure_and_budget():
    assert recovery_action(category="business_failure", planner_iteration=1, limit=10) == "replan"
    assert (
        recovery_action(category="business_failure", planner_iteration=10, limit=10)
        == "terminate_budget_exhausted"
    )
    assert (
        recovery_action(
            category="transient_infrastructure",
            planner_iteration=1,
            limit=10,
        )
        == "retry_node"
    )
    assert recovery_action(category="terminal_failure", planner_iteration=1, limit=10) == "terminate"


def test_langgraph_send_merges_parallel_worker_results():
    def worker(state):
        task = state["active_task"]
        return {
            "task_results": [
                {
                    "task_id": task["id"],
                    "status": "success",
                    "content": task["task_goal"],
                    "error_code": None,
                }
            ]
        }

    graph = StateGraph(AgentState)
    graph.add_node("task_scheduler", task_scheduler_node)
    graph.add_node("analysis_agent", worker)
    graph.add_node("finish", lambda _state: {})
    graph.set_entry_point("task_scheduler")
    graph.add_conditional_edges(
        "task_scheduler",
        _dispatch_after_scheduler,
        {"synthesizer": "finish", "planner_agent": "finish", "approval_gate": "finish"},
    )
    graph.add_edge("analysis_agent", "task_scheduler")
    graph.add_edge("finish", END)
    compiled = graph.compile()
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析两项指标",
    )
    state["intent"] = {
        "intent_type": "financial_research",
        "domain": "a_share_equity_research",
        "subdomain": "company",
        "goal": "分析两项指标",
        "confidence": 1.0,
    }
    state["planner_iteration"] = 1
    state["plan"] = {
        "tasks": [
            {"id": "one", "task_goal": "第一项", "depends_on": []},
            {"id": "two", "task_goal": "第二项", "depends_on": []},
        ]
    }

    result = compiled.invoke(state)

    assert {item["task_id"] for item in result["task_results"]} == {"one", "two"}


class _ParallelSkillRegistry:
    def get_definition_layers(self, *, name):
        return (
            SkillDefinition(
                name=name,
                version="1.0.0",
                description=f"执行 {name}",
                definition_hash=f"{name}-hash",
            ),
            SkillExecutionGuide(
                name=name,
                version="1.0.0",
                instructions=[f"完成 {name}"],
                definition_hash=f"{name}-hash",
            ),
        )


class _ParallelSkillLlm:
    async def complete(self, **kwargs):
        return LlmCompletionResult(
            content=kwargs["user_prompt"],
            model="fake",
            provider="fake",
        )


def test_langgraph_send_merges_two_real_skill_workers_without_scalar_conflict():
    graph = StateGraph(AgentState)
    graph.add_node("task_scheduler", task_scheduler_node)
    graph.add_node(
        "skill_agent",
        build_skill_agent_node(
            registry=_ParallelSkillRegistry(),
            llm_client=_ParallelSkillLlm(),
        ),
    )
    graph.add_node("finish", lambda _state: {})
    graph.set_entry_point("task_scheduler")
    graph.add_conditional_edges(
        "task_scheduler",
        _dispatch_after_scheduler,
        {"synthesizer": "finish", "planner_agent": "finish", "approval_gate": "finish"},
    )
    graph.add_edge("skill_agent", "task_scheduler")
    graph.add_edge("finish", END)
    state = create_initial_state(
        session_id="session-skills",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="并行分析两个维度",
    )
    state["planner_iteration"] = 1
    state["plan"] = {
        "tasks": [
            {"id": "quality", "task_goal": "质量", "skill_name": "quality"},
            {"id": "risk", "task_goal": "风险", "skill_name": "risk"},
        ]
    }

    result = graph.compile().invoke(state)

    assert {item["task_id"] for item in result["task_results"]} == {
        "quality",
        "risk",
    }
