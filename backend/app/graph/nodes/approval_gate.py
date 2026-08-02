from hashlib import sha256
from typing import Any

from langgraph.types import interrupt

from app.graph.state import AgentState
from app.schemas.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovedToolCall,
    PendingToolApproval,
)
from app.schemas.tools import ToolCallRequest
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.registry import ToolCatalogChangedError


class ApprovalResumeError(ValueError):
    code = "approval_resume_invalid"
    http_status = 409


class ApprovalPayloadMismatchError(ApprovalResumeError):
    code = "approval_payload_mismatch"


def approval_gate_node(state: AgentState) -> AgentState:
    descriptor = state["pending_tool_approval"]
    if descriptor is None:
        return state
    request = _descriptor_request(state, descriptor)
    arguments_hash = tool_call_fingerprint(request)
    if arguments_hash != descriptor.request_fingerprint:
        raise ApprovalPayloadMismatchError("工具参数已变化，待审批描述符无效。")
    approval_id = _approval_id(
        state["request_id"], descriptor.call_id, arguments_hash
    )
    safe_arguments = descriptor.safe_arguments
    approval_request = ApprovalRequest(
        approval_id=approval_id,
        run_id=state["request_id"],
        approval_type="tool_call",
        risk_level=descriptor.risk_level,
        payload={
            "call_id": descriptor.call_id,
            "tool_name": descriptor.tool_name,
            "tool_version": descriptor.tool_version,
            "arguments_hash": arguments_hash,
            "arguments": safe_arguments,
            "purpose": descriptor.purpose,
        },
    )
    _upsert_approval_request(state, approval_request)
    state["approval_status"] = ApprovalStatus(required=True, status="pending")

    decision = interrupt(
        {
            "approval_id": approval_id,
            "approval_type": "tool_call",
            "run_id": state["request_id"],
            "session_id": state["session_id"],
            "tenant_id": state["tenant_id"],
            "request_user_id": state["user_id"],
            "call_id": descriptor.call_id,
            "tool_name": descriptor.tool_name,
            "tool_version": descriptor.tool_version,
            "arguments_hash": arguments_hash,
            "arguments": safe_arguments,
            "purpose": descriptor.purpose,
            "checkpoint_thread_id": state["session_id"],
            "status": "pending",
        }
    )
    _validate_decision(decision, approval_id=approval_id, arguments_hash=arguments_hash)
    approval_request.status = decision["decision"]
    _upsert_approval_request(state, approval_request)
    state["pending_tool_approval"] = None

    if decision["decision"] == "rejected":
        state["approval_status"] = ApprovalStatus(required=True, status="rejected")
        state["plan"] = {
            **(state["plan"] or {}),
            "execution_mode": "direct_response",
            "requested_tools": [],
            "response_policy": {"response_type": "approval_rejected_response"},
        }
        return state

    approval = ApprovedToolCall(
        call_id=descriptor.call_id,
        arguments_hash=arguments_hash,
    )
    if approval not in state["approved_tool_calls"]:
        state["approved_tool_calls"].append(approval)
    state["approval_status"] = ApprovalStatus(required=True, status="approved")
    return state


def build_approval_gate_node(*, tool_catalog_digest: str):
    def node(state: AgentState) -> AgentState:
        if state.get("tool_catalog_digest") != tool_catalog_digest:
            raise ToolCatalogChangedError()
        return approval_gate_node(state)

    return node


def _descriptor_request(
    state: AgentState, descriptor: PendingToolApproval
) -> ToolCallRequest:
    plan = state["plan"] or {}
    requests = plan.get("requested_tools", [])
    if descriptor.step_index >= len(requests):
        raise ApprovalPayloadMismatchError("待审批工具步骤不存在。")
    raw_request = requests[descriptor.step_index]
    request = (
        raw_request
        if isinstance(raw_request, ToolCallRequest)
        else ToolCallRequest.model_validate(raw_request)
    )
    if (
        request.call_id != descriptor.call_id
        or request.tool_name != descriptor.tool_name
        or request.tool_version != descriptor.tool_version
    ):
        raise ApprovalPayloadMismatchError("待审批工具调用实例不匹配。")
    return request


def _approval_id(run_id: str, call_id: str, arguments_hash: str) -> str:
    digest = sha256(f"{run_id}:{call_id}:{arguments_hash}".encode()).hexdigest()
    return f"approval_{digest[:32]}"


def _validate_decision(
    decision: Any,
    *,
    approval_id: str,
    arguments_hash: str,
) -> None:
    if not isinstance(decision, dict):
        raise ApprovalResumeError("审批恢复数据必须是对象。")
    if decision.get("approval_id") != approval_id:
        raise ApprovalPayloadMismatchError("审批记录与当前工具调用不匹配。")
    if decision.get("arguments_hash") != arguments_hash:
        raise ApprovalPayloadMismatchError("工具参数已变化，原审批不可复用。")
    if decision.get("decision") not in {"approved", "rejected"}:
        raise ApprovalResumeError("审批决定必须为 approved 或 rejected。")


def _upsert_approval_request(
    state: AgentState,
    approval_request: ApprovalRequest,
) -> None:
    state["approval_requests"] = [
        existing
        for existing in state["approval_requests"]
        if existing.approval_id != approval_request.approval_id
    ]
    state["approval_requests"].append(approval_request)
