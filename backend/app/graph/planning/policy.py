from dataclasses import dataclass

from app.graph.planning.contracts import PlanModel


MAX_PLANNER_ITERATIONS = 2
MAX_WEB_SEARCH_TASKS = 6
MAX_PLANNER_FEEDBACKS = 24
PLANNER_POLICY_VERSION = "model-feedback-v1"


@dataclass(frozen=True)
class PlannerMode:
    thinking: bool
    max_tokens: int


def planner_mode(current_iteration: int) -> PlannerMode:
    if current_iteration > 0:
        return PlannerMode(thinking=True, max_tokens=2048)
    return PlannerMode(thinking=False, max_tokens=1024)


def remaining_planner_iterations(current_iteration: int) -> int:
    return max(MAX_PLANNER_ITERATIONS - current_iteration, 0)


def prune_redundant_terminal_analysis(plan: PlanModel) -> PlanModel:
    """兼容旧调用；拓扑不代表业务冗余，不静默修改模型计划。"""
    return plan


def observation_signature(plan: PlanModel, results: list[dict]) -> str:
    from hashlib import sha256
    import json

    ids = {task.id for task in plan.tasks}
    observations = sorted(
        (item for item in results if item.get("task_id") in ids), key=lambda item: item["task_id"]
    )
    return sha256(
        json.dumps(
            {"plan": plan.model_dump(), "results": observations}, sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()
