from app.graph.planning.contracts import PlanModel
from app.graph.planning.policy import (
    PlannerMode,
    planner_mode,
    prune_redundant_terminal_analysis,
    remaining_planner_iterations,
)


def test_initial_planner_mode_prioritizes_direct_structured_output():
    assert planner_mode(0) == PlannerMode(thinking=False, max_tokens=2048)


def test_replanner_mode_prioritizes_direct_structured_output():
    assert planner_mode(1) == PlannerMode(thinking=False, max_tokens=2048)


def test_effective_remaining_iterations_uses_runtime_limit():
    assert remaining_planner_iterations(0) == 2
    assert remaining_planner_iterations(1) == 1
    assert remaining_planner_iterations(2) == 0


def test_preserves_terminal_analysis_without_semantic_deletion():
    plan = PlanModel.model_validate(
        {
            "tasks": [
                {"id": "search", "task_goal": "搜索"},
                {
                    "id": "judge",
                    "task_goal": "综合判断",
                    "depends_on": ["search"],
                },
            ]
        }
    )

    optimized = prune_redundant_terminal_analysis(plan)

    assert [task.id for task in optimized.tasks] == ["search", "judge"]


def test_keeps_analysis_consumed_by_later_task():
    plan = PlanModel.model_validate(
        {
            "tasks": [
                {"id": "search", "task_goal": "搜索"},
                {
                    "id": "normalize",
                    "task_goal": "标准化",
                    "depends_on": ["search"],
                },
                {
                    "id": "compare",
                    "task_goal": "比较",
                    "depends_on": ["normalize"],
                    "skill_name": "compare",
                },
            ]
        }
    )

    assert [task.id for task in prune_redundant_terminal_analysis(plan).tasks] == [
        "search",
        "normalize",
        "compare",
    ]


def test_keeps_single_analysis_for_question_without_external_capability():
    plan = PlanModel.model_validate(
        {"tasks": [{"id": "explain", "task_goal": "解释市盈率"}]}
    )

    assert [task.id for task in prune_redundant_terminal_analysis(plan).tasks] == [
        "explain"
    ]
