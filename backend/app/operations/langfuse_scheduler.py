"""调度观测的最小内容投影；不改变业务 State 或 Send 参数。"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from langgraph.types import Send

from app.graph.planning.contracts import PlanModel, TaskResultModel
from app.graph.planning.policy import MAX_PLANNER_FEEDBACKS, observation_signature
from app.graph.planning.request_flow import uses_request_flow
from app.graph.planning.scheduler import ready_tasks

logger = logging.getLogger("bs_agent.langfuse")

_ROUTE_EXPLANATIONS = {
    "permission_recovery_confirmed": "消费已持久化的批次确认，仅执行已列出的替代取证。",
    "settled_peer_allows_deferred_dispatch": "同批已结算，继续尚未远端派发的原参数调用，不重新选择工具。",
    "user_input_required": "存在待用户确认请求，先等待用户输入。",
    "permission_fallback_resume": "先消费用户对权限回退的确认，恢复原工具任务。",
    "approval_required": "存在待审批操作，先进入审批节点。",
    "skill_workflow_failed": "Skill 工作流已失败，转入回答汇总。",
    "skill_workflow_dispatch": "继续执行已有 Skill 工作流的下一步骤。",
    "planner_stopped": "已设置规划停止原因，转入回答汇总。",
    "planner_finished": "Planner 已评审当前结果并决定结束。",
    "planner_feedback_budget_exhausted": "当前结果尚未评审，但 Planner 反馈预算已耗尽。",
    "new_results_require_review": "当前计划产生了尚未评审的新结果；成功结果也需要评审。",
    "no_ready_tasks": "当前没有可继续执行的就绪任务，转入回答汇总。",
    "skill_task_ready": "存在就绪 Skill 任务，按现有策略串行启动。",
    "ready_tasks": "按依赖派发就绪任务。",
    "replan_request_unbound": "没有绑定当前计划的修订申请，结束规划。",
    "replan_output_invalid": "修订输出无效，保留有效证据并结束取证。",
    "replan_no_effective_change": "申请未产生可执行的实质修订。",
}

_RECOVERY_EVENTS = frozenset(
    {
        "result_classified",
        "receipt_settled",
        "availability_skip",
        "recovery_batch_sealed",
        "recovery_decision",
        "recovery_invalidated",
        "recovery_dispatch",
        "recovery_result",
    }
)
_RECOVERY_FIELDS = frozenset(
    {
        "request_id",
        "original_call_id",
        "replacement_call_id",
        "batch_id",
        "rule_id",
        "execution_state",
        "settlement_reason",
        "availability_scope",
        "budget_disposition",
        "coverage_status",
        "route_reason",
    }
)
_MACHINE_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def record_recovery_event(event: str, **fields: Any) -> None:
    """只导出有界机器引用和原因码，观测失败不改变业务/回执。"""
    if event not in _RECOVERY_EVENTS:
        return
    try:
        from app.operations.langfuse_observability import (
            langfuse_observation,
            update_langfuse_observation,
        )

        payload = {
            "event": event,
            **{
                key: value
                for key, value in fields.items()
                if key in _RECOVERY_FIELDS
                and isinstance(value, str)
                and _MACHINE_VALUE.fullmatch(value)
            },
        }
        with langfuse_observation(as_type="span", name=event) as observation:
            update_langfuse_observation(observation, bounded_content=True, output=payload)
    except Exception as exc:  # noqa: BLE001 - 只记录错误类型，不回退查询、正文或原始错误
        logger.warning("langfuse_recovery_event_failed", extra={"error_type": type(exc).__name__})


def _field(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _recovery_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "policy_version": state.get("recovery_policy_version"),
        "batches": [
            {
                "batch_id": batch_id,
                "original_call_ids": [item["original_call_id"] for item in batch.get("items", [])],
            }
            for batch_id, batch in sorted(state.get("recovery_batches", {}).items())
        ],
        "outcomes": [
            {
                "original_call_id": call_id,
                **{
                    key: outcome[key]
                    for key in (
                        "batch_id",
                        "replacement_call_id",
                        "status",
                        "error_code",
                        "coverage_status",
                    )
                    if key in outcome
                },
            }
            for call_id, outcome in sorted(state.get("recovery_outcomes", {}).items())
        ],
        "dispatches": [
            {"dispatch_batch_id": batch_id, "members": batch.get("members", [])}
            for batch_id, batch in sorted(state.get("dispatch_batches", {}).items())
        ],
        "search_budget_authority": "durable_receipts",
    }


def scheduler_trace_input(state: Mapping[str, Any]) -> dict[str, Any]:
    """仅保留决策所需结构，不包含结果正文、工具参数或确认内容。"""
    raw_plan = state.get("plan")
    plan = PlanModel.model_validate(raw_plan) if raw_plan else None
    results = state.get("task_results", [])
    active_ids = {task.id for task in plan.tasks} if plan else set()
    completed_ids = {item["task_id"] for item in results if item["task_id"] in active_ids}
    ready = (
        ready_tasks(
            plan,
            results=[TaskResultModel.model_validate(item) for item in results],
        )
        if plan
        else []
    )
    ready_ids = {task.id for task in ready}
    signature = observation_signature(plan, results) if plan else None
    feedback_count = state.get("planner_feedback_count", 0)
    return {
        **({"recovery": _recovery_summary(state)} if state.get("recovery_policy_version") else {}),
        "plan": {
            "tasks": [
                {
                    "id": task.id,
                    "task_goal": task.task_goal,
                    "execution_type": task.execution_type,
                    "skill_name": task.skill_name,
                    "depends_on": list(task.depends_on),
                    **({"tool_candidate_refs": list(task.tool_candidate_refs)} if task.tool_candidate_refs else {}),
                }
                for task in plan.tasks
            ],
        }
        if plan
        else None,
        "task_results": [
            {
                "task_id": item["task_id"],
                "status": item["status"],
                "error_code": item.get("error_code"),
                "tool_name": item.get("tool_name"),
                "in_current_plan": item["task_id"] in active_ids,
            }
            for item in results
        ],
        "ready_task_ids": [task.id for task in ready],
        "blocked_task_ids": [
            task.id
            for task in plan.tasks
            if task.id not in completed_ids and task.id not in ready_ids
        ]
        if plan
        else [],
        "has_observations": bool(completed_ids),
        "planner_iteration": state.get("planner_iteration", 0),
        **({"request_flow": {
            "version": state.get("request_flow_version"),
            "replan_request_count": state.get("replan_request_count", 0),
            "pending_replan": state.get("pending_replan") is not None,
            "stop_reason": state.get("replan_stop_reason"),
        }} if uses_request_flow(state) else {
            "reviewed": bool(signature and state.get("planner_reviewed_signature") == signature),
            "planner_feedback_count": feedback_count,
            "planner_feedback_limit": MAX_PLANNER_FEEDBACKS,
            "planner_feedback_remaining": max(0, MAX_PLANNER_FEEDBACKS - feedback_count),
        }),
        "planner_decision": {
            key: value
            for key, value in (state.get("planner_decision") or {}).items()
            if key in {"action", "reason", "synthesis_task_id"}
        },
        "planner_stop_reason": state.get("planner_stop_reason"),
        "tool_search_count": state.get("tool_search_count", 0),
        "pending_user_input": state.get("pending_user_input") is not None,
        "user_input_response_received": state.get("user_input_response") is not None,
        "pending_tool_approval": state.get("pending_tool_approval") is not None,
        "pending_memory_approval": any(
            _field(item, "approval_status") == "pending"
            and _field(item, "sensitivity") in {"medium", "high"}
            for item in state.get("memory_candidates", [])
        ),
        "skill_workflows": [
            {
                "parent_task_id": task_id,
                "status": _field(workflow, "status"),
                "checked_steps": _field(workflow, "checked_steps", []),
            }
            for task_id, workflow in state.get("skill_workflows", {}).items()
        ],
    }


def scheduler_trace_update(update: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: value
        for key, value in update.items()
        if key in {"tool_search_count", "planner_stop_reason"}
    }
    if "pending_user_input" in update:
        result["pending_user_input"] = update["pending_user_input"] is not None
    if "recovery_batches" in update or "recovery_outcomes" in update:
        result["recovery"] = _recovery_summary(update)
    return result


def record_scheduler_input(observation: Any, state: Mapping[str, Any]) -> None:
    if observation is None:
        return
    try:
        from app.operations.langfuse_observability import update_langfuse_observation

        update_langfuse_observation(
            observation,
            bounded_content=True,
            input=scheduler_trace_input(state),
        )
    except Exception as exc:  # noqa: BLE001 - 观测摘要失败不能影响业务路由
        logger.warning("langfuse_scheduler_input_failed", extra={"error_type": type(exc).__name__})


def record_scheduler_dispatch(
    observation: Any,
    destination: str | list[Send],
    reason: str,
    **facts: Any,
) -> str | list[Send]:
    """在实际分支返回点记录原因，始终原样返回 destination。"""
    if observation is None:
        return destination
    try:
        from app.operations.langfuse_observability import update_langfuse_observation

        sends = destination if isinstance(destination, list) else []
        nodes = list(dict.fromkeys(send.node for send in sends)) if sends else [destination]
        update_langfuse_observation(
            observation,
            bounded_content=True,
            output={
                "next_node": nodes[0] if len(nodes) == 1 else None,
                "next_nodes": nodes,
                "reason": reason,
                "explanation": _ROUTE_EXPLANATIONS[reason],
                "dispatched_tasks": [
                    {
                        "node": send.node,
                        "task_id": (send.arg.get("active_task") or {}).get("id"),
                        "active_skill_step": send.arg.get("active_skill_step"),
                        **(
                            {"web_search_allowed": send.arg["web_search_allowed"]}
                            if "web_search_allowed" in send.arg
                            else {}
                        ),
                    }
                    for send in sends
                ],
                **facts,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 观测导出失败不能影响业务返回值
        logger.warning("langfuse_scheduler_output_failed", extra={"error_type": type(exc).__name__})
    return destination
