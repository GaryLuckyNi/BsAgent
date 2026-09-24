from collections.abc import Sequence
from typing import Literal

from app.graph.planning.contracts import PlanModel, SubTaskModel, TaskResultModel


PlanStatus = Literal["running", "complete", "needs_review", "blocked"]
FailureCategory = Literal[
    "business_failure",
    "transient_infrastructure",
    "terminal_failure",
]
RecoveryAction = Literal[
    "review",
    "retry_node",
    "terminate",
    "terminate_budget_exhausted",
]


def ready_tasks(
    plan: PlanModel,
    *,
    results: Sequence[TaskResultModel],
) -> list[SubTaskModel]:
    task_ids = {task.id for task in plan.tasks}
    result_by_id = {result.task_id: result for result in results if result.task_id in task_ids}
    ready: list[SubTaskModel] = []
    for task in plan.tasks:
        if task.id in result_by_id:
            continue
        dependencies = [result_by_id.get(task_id) for task_id in task.depends_on]
        if all(result is not None for result in dependencies) and all(
            result.status == "success" for result in dependencies
        ):
            ready.append(task)
    return ready


def plan_status(
    plan: PlanModel,
    *,
    results: Sequence[TaskResultModel],
) -> PlanStatus:
    task_ids = {task.id for task in plan.tasks}
    result_by_id = {result.task_id: result for result in results if result.task_id in task_ids}
    if ready_tasks(plan, results=results):
        return "running"
    if all(task.id in result_by_id for task in plan.tasks):
        return "complete"
    return "blocked"


def recovery_action(
    *,
    category: FailureCategory,
    planner_iteration: int,
    limit: int,
) -> RecoveryAction:
    if category == "transient_infrastructure":
        return "retry_node"
    if planner_iteration >= limit:
        return "terminate_budget_exhausted"
    return "review"
