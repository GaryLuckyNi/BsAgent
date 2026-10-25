"""请求级修订的控制边界；证据充分性由合成模型判断。"""

import json
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.graph.planning.contracts import PlanModel, TaskResultModel
from app.graph.planning.policy import MAX_PLANNER_ITERATIONS
from app.graph.planning.scheduler import ready_tasks

REQUEST_FLOW_VERSION = "context-routing-v1"
MAX_REPLAN_REQUESTS = 1


def uses_request_flow(state: dict) -> bool:
    return state.get("request_flow_version") == REQUEST_FLOW_VERSION


def plan_signature(plan: dict | None) -> str:
    return sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ReplanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason_type: Literal[
        "missing_task", "new_evidence", "evidence_conflict", "path_invalid", "premise_invalid"
    ]
    affected_goal: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=16)
    existing_plan_limitation: str = Field(min_length=1, max_length=1000)
    required_changes: list[str] = Field(min_length=1, max_length=8)


def replan_block_reason(state: dict) -> str | None:
    if not uses_request_flow(state) or not state.get("plan"):
        return "replan_no_active_plan"
    if state.get("replan_stop_reason"):
        return state["replan_stop_reason"]
    if state.get("replan_request_count", 0) >= MAX_REPLAN_REQUESTS:
        return "replan_budget_exhausted"
    if state.get("planner_iteration", 0) >= min(settings.planner_max_iterations, MAX_PLANNER_ITERATIONS):
        return "replan_budget_exhausted"
    if (state.get("pending_user_input") or state.get("user_input_response")
            or state.get("pending_tool_approval")):
        return "replan_pending_execution"
    if any(value.get("status") == "running" for value in state.get("skill_workflows", {}).values()):
        return "replan_pending_execution"
    if any(record.result is None for record in state.get("tool_calls", [])):
        return "replan_pending_execution"
    plan = PlanModel.model_validate(state["plan"])
    results = [TaskResultModel.model_validate(value) for value in state.get("task_results", [])]
    if ready_tasks(plan, results=results):
        return "replan_pending_execution"
    return None


def evidence_refs(state: dict) -> set[str]:
    from app.graph.planning.context import user_materials

    return {
        *(item["ref"] for item in user_materials(state)),
        *(f"task:{item['task_id']}" for item in state.get("task_results", [])),
        *(f"tool:{item.request.call_id}" for item in state.get("tool_calls", [])),
    }


def accept_replan(state: dict, request: dict) -> str:
    """写入绑定于本轮计划的申请，返回下一节点；拒绝不再循环申请。"""
    reason = replan_block_reason(state)
    try:
        parsed = ReplanRequest.model_validate(request)
    except ValueError:
        parsed = None
        reason = reason or "replan_request_invalid"
    if parsed is not None and not set(parsed.evidence_refs) <= evidence_refs(state):
        reason = reason or "replan_evidence_ref_invalid"
    state["replan_request_count"] = state.get("replan_request_count", 0) + 1
    state["synthesis_decision"] = None
    if reason:
        state["pending_replan"] = None
        state["replan_stop_reason"] = reason
        return "synthesizer"
    state["pending_replan"] = {
        "request_id": state["request_id"],
        "plan_signature": plan_signature(state["plan"]),
        "request": parsed.model_dump(mode="json"),
    }
    return "planner_agent"


def pending_replan_valid(state: dict) -> bool:
    pending = state.get("pending_replan") or {}
    return (pending.get("request_id") == state.get("request_id")
            and pending.get("plan_signature") == plan_signature(state.get("plan"))
            and isinstance(pending.get("request"), dict))
