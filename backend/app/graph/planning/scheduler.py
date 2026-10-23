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


def synthesis_handoff_task(
    plan: PlanModel, *, results: Sequence[TaskResultModel], task_id: str,
) -> SubTaskModel:
    """只接受显式交接，不能据此跳过工具、中间分析或未成功依赖。"""
    completed = {result.task_id for result in results}
    pending = [task for task in plan.tasks if task.id not in completed]
    if len(pending) != 1 or pending[0].id != task_id:
        raise ValueError("最终回答只能承接唯一剩余的终端分析任务。")
    task = pending[0]
    if task.execution_type != "analysis" or task.skill_name is not None:
        raise ValueError("最终回答不可承接工具或 Skill 执行任务。")
    if any(task_id in item.depends_on for item in plan.tasks):
        raise ValueError("分析结果还有后续消费者，必须保留独立执行。")
    if task_id not in {item.id for item in ready_tasks(plan, results=results)}:
        raise ValueError("最终分析的执行依赖尚未成功完成。")
    return task


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
