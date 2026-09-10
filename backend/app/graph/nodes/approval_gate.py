from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Literal

from langgraph.types import interrupt

from app.graph.state import AgentState, build_checkpoint_thread_id
from app.schemas.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovedToolCall,
    PendingToolApproval,
)
from app.schemas.memory import MemoryCandidate
from app.schemas.tools import ToolCallRequest
from app.graph.planning.contracts import TaskResultModel
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.registry import ToolCatalogChangedError


@dataclass(frozen=True)
class ApprovalAction:
    """The complete approval action, persisted only by LangGraph's checkpointer."""

    approval_type: Literal["tool_call", "memory_write"]
    subject_name: str
    risk_level: Literal["low", "medium", "high"]
    action_digest: str
    interrupt_payload: dict[str, Any]


class ApprovalResumeError(ValueError):
    code = "approval_resume_invalid"
    http_status = 409


class ApprovalPayloadMismatchError(ApprovalResumeError):
    code = "approval_payload_mismatch"


def approval_gate_node(state: AgentState) -> AgentState:
    action = build_approval_action(state)
    if action is None:
        return state

    payload = action.interrupt_payload
    _upsert_approval_request(
        state,
        ApprovalRequest(
            approval_id=payload["approval_id"],
            run_id=state["request_id"],
            approval_type=action.approval_type,
            risk_level=action.risk_level,
            payload=payload,
        ),
    )
    state["approval_status"] = ApprovalStatus(required=True, status="pending")
    decision = interrupt(payload)
    _validate_decision(
        decision,
        approval_id=payload["approval_id"],
        action_digest=action.action_digest,
    )
    _apply_decision(state, action=action, decision=decision)
    return state


def build_approval_action(state: AgentState) -> ApprovalAction | None:
    """Return the next action in stable order: governed memory before tools."""
    candidate = _next_pending_memory_candidate(state)
    if candidate is not None:
        return _memory_action(state, candidate)
    descriptor = state["pending_tool_approval"]
    if descriptor is not None:
        return _tool_action(state, descriptor)
    return None


def build_approval_gate_node(*, tool_catalog_digest: str):
    def node(state: AgentState) -> AgentState:
        if state.get("tool_catalog_digest") != tool_catalog_digest:
            raise ToolCatalogChangedError()
        return approval_gate_node(state)

    return node


def _next_pending_memory_candidate(state: AgentState) -> MemoryCandidate | None:
    candidates = [
        candidate
        for candidate in state["memory_candidates"]
        if candidate.approval_status == "pending"
        and candidate.sensitivity in {"medium", "high"}
    ]
    return min(candidates, key=lambda item: (item.memory_key or "", item.candidate_id), default=None)


def _memory_action(state: AgentState, candidate: MemoryCandidate) -> ApprovalAction:
    subject_name = candidate.memory_key or candidate.candidate_id
    canonical_action = {
        "approval_type": "memory_write",
        "candidate_id": candidate.candidate_id,
        "memory_key": subject_name,
        "memory_type": candidate.memory_type,
        "content": candidate.content,
        "confidence": candidate.confidence,
        "sensitivity": candidate.sensitivity,
        "source": candidate.source,
    }
    action_digest = _action_digest(canonical_action)
    approval_id = _approval_id(state["request_id"], action_digest)
    return ApprovalAction(
        approval_type="memory_write",
        subject_name=subject_name,
        risk_level=candidate.sensitivity,
        action_digest=action_digest,
        interrupt_payload={
            **canonical_action,
            "approval_id": approval_id,
            "run_id": state["request_id"],
            "tenant_id": state["tenant_id"],
            "requested_by": state["user_id"],
            "subject_name": subject_name,
            "risk_level": candidate.sensitivity,
            "action_digest": action_digest,
            "checkpoint_thread_id": build_checkpoint_thread_id(
                tenant_id=state["tenant_id"],
                session_key=state["session_key"],
                request_id=state["request_id"],
            ),
            "checkpoint_namespace": "",
            "status": "pending",
        },
    )


def _tool_action(state: AgentState, descriptor: PendingToolApproval) -> ApprovalAction:
    request = _descriptor_request(state, descriptor)
    arguments_hash = tool_call_fingerprint(request)
    if arguments_hash != descriptor.request_fingerprint:
        raise ApprovalPayloadMismatchError("工具参数已变化，待审批描述符无效。")
    canonical_action = {
        "approval_type": "tool_call",
        "call_id": descriptor.call_id,
        "tool_name": descriptor.tool_name,
        "tool_version": descriptor.tool_version,
        "arguments": request.arguments,
        "purpose": descriptor.purpose,
    }
    action_digest = _action_digest(canonical_action)
    approval_id = _approval_id(state["request_id"], action_digest)
    return ApprovalAction(
        approval_type="tool_call",
        subject_name=descriptor.tool_name,
        risk_level=descriptor.risk_level,
        action_digest=action_digest,
        interrupt_payload={
            **canonical_action,
            "approval_id": approval_id,
            "run_id": state["request_id"],
            "tenant_id": state["tenant_id"],
            "requested_by": state["user_id"],
            "subject_name": descriptor.tool_name,
            "risk_level": descriptor.risk_level,
            "arguments_hash": arguments_hash,
            "action_digest": action_digest,
            "checkpoint_thread_id": build_checkpoint_thread_id(
                tenant_id=state["tenant_id"],
                session_key=state["session_key"],
                request_id=state["request_id"],
            ),
            "checkpoint_namespace": "",
            "status": "pending",
        },
    )


def _apply_decision(state: AgentState, *, action: ApprovalAction, decision: dict[str, Any]) -> None:
    if action.approval_type == "memory_write":
        candidate_id = action.interrupt_payload["candidate_id"]
        for candidate in state["memory_candidates"]:
            if candidate.candidate_id == candidate_id:
                candidate.approval_status = decision["decision"]
                break
        # A rejected preference must not reject an unrelated planned tool call.
        state["approval_status"] = None
        return

    descriptor = state["pending_tool_approval"]
    if descriptor is None:
        raise ApprovalPayloadMismatchError("待审批工具调用不存在。")
    state["pending_tool_approval"] = None
    if decision["decision"] == "rejected":
        state["approval_status"] = ApprovalStatus(required=True, status="rejected")
        state["task_results"].append(
            TaskResultModel(
                task_id=descriptor.task_id,
                status="terminal_failure",
                content="用户拒绝了该工具调用。",
                error_code="tool_approval_rejected",
            ).model_dump(mode="python")
        )
        return
    approval = ApprovedToolCall(
        call_id=descriptor.call_id,
        arguments_hash=action.interrupt_payload["arguments_hash"],
    )
    if approval not in state["approved_tool_calls"]:
        state["approved_tool_calls"].append(approval)
    state["approval_status"] = ApprovalStatus(required=True, status="approved")


def _descriptor_request(state: AgentState, descriptor: PendingToolApproval) -> ToolCallRequest:
    record = next(
        (
            item
            for item in state["tool_calls"]
            if (item.call_id or item.request.call_id) == descriptor.call_id
            and item.result is None
        ),
        None,
    )
    if record is None:
        raise ApprovalPayloadMismatchError("待审批工具调用不存在。")
    request = record.request
    if (request.call_id, request.tool_name, request.tool_version) != (
        descriptor.call_id,
        descriptor.tool_name,
        descriptor.tool_version,
    ):
        raise ApprovalPayloadMismatchError("待审批工具调用实例不匹配。")
    return request


def _action_digest(action: dict[str, Any]) -> str:
    serialized = json.dumps(action, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode("utf-8")).hexdigest()


def _approval_id(run_id: str, action_digest: str) -> str:
    digest = sha256(f"{run_id}:{action_digest}".encode()).hexdigest()
    return f"approval_{digest[:32]}"


def _validate_decision(decision: Any, *, approval_id: str, action_digest: str) -> None:
    if not isinstance(decision, dict):
        raise ApprovalResumeError("审批恢复数据必须是对象。")
    if decision.get("approval_id") != approval_id or decision.get("action_digest") != action_digest:
        raise ApprovalPayloadMismatchError("审批记录与当前中断动作不匹配。")
    if decision.get("decision") not in {"approved", "rejected"}:
        raise ApprovalResumeError("审批决定必须为 approved 或 rejected。")


def _upsert_approval_request(state: AgentState, approval_request: ApprovalRequest) -> None:
    state["approval_requests"] = [
        existing for existing in state["approval_requests"]
        if existing.approval_id != approval_request.approval_id
    ]
    state["approval_requests"].append(approval_request)
