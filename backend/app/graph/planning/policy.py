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
    del current_iteration
    # 规划器输出受严格 JSON 契约约束。禁用隐藏推理可让模型直接
    # 生成结构化计划，避免在首个可公开摘要之前消耗数秒 reasoning token。
    return PlannerMode(thinking=False, max_tokens=2048)


def remaining_planner_iterations(current_iteration: int) -> int:
    return max(MAX_PLANNER_ITERATIONS - current_iteration, 0)


def prune_redundant_terminal_analysis(plan: PlanModel) -> PlanModel:
    """兼容旧调用；拓扑不代表业务冗余，不静默修改模型计划。"""
    return plan


def observation_signature(plan: PlanModel, results: list[dict]) -> str:
    import json
    from hashlib import sha256

    ids = {task.id for task in plan.tasks}
    observations = sorted(
        (item for item in results if item.get("task_id") in ids), key=lambda item: item["task_id"]
    )
    plan_payload = plan.model_dump()
    # 新增的可选空字段不改变旧 checkpoint 的评审签名。
    for task in plan_payload["tasks"]:
        if not task.get("tool_candidate_refs"):
            task.pop("tool_candidate_refs", None)
        if task.get("proposed_tool_call") is None:
            task.pop("proposed_tool_call", None)
    return sha256(
        json.dumps(
            {"plan": plan_payload, "results": observations}, sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()
