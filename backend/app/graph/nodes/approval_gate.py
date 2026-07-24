from hashlib import sha256
from typing import Any

from langgraph.types import interrupt

from app.graph.state import AgentState
from app.schemas.approvals import ApprovalRequest, ApprovalStatus
from app.schemas.tools import ToolCallRequest
from app.tools.fingerprints import tool_call_fingerprint


SENSITIVE_ARGUMENT_KEYS = {
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
}


class ApprovalResumeError(ValueError):
    code = "approval_resume_invalid"
    http_status = 409


class ApprovalPayloadMismatchError(ApprovalResumeError):
    code = "approval_payload_mismatch"


def approval_gate_node(state: AgentState) -> AgentState:
    for request in _pending_approval_requests(state):
        arguments_hash = tool_call_fingerprint(request)
        approval_id = _approval_id(state["request_id"], arguments_hash)
        approval_request = ApprovalRequest(
            approval_id=approval_id,
            run_id=state["request_id"],
            approval_type="tool_call",
            risk_level="high",
            payload={
                "tool_name": request.tool_name,
                "tool_version": request.tool_version,
                "arguments_hash": arguments_hash,
                "arguments": _redact_arguments(request.arguments),
                "purpose": request.purpose,
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
                "tool_name": request.tool_name,
                "tool_version": request.tool_version,
                "arguments_hash": arguments_hash,
                "arguments": _redact_arguments(request.arguments),
                "purpose": request.purpose,
                "checkpoint_thread_id": state["request_id"],
                "status": "pending",
            }
        )
        _validate_decision(
            decision,
            approval_id=approval_id,
            arguments_hash=arguments_hash,
        )
        approval_request.status = decision["decision"]
        _upsert_approval_request(state, approval_request)

        if decision["decision"] == "rejected":
            state["approval_status"] = ApprovalStatus(required=True, status="rejected")
            state["plan"] = {
                **(state["plan"] or {}),
                "execution_mode": "direct_response",
                "requested_tools": [],
                "response_policy": {"response_type": "approval_rejected_response"},
            }
            return state

        if arguments_hash not in state["approved_tool_call_hashes"]:
            state["approved_tool_call_hashes"].append(arguments_hash)
        state["approval_status"] = ApprovalStatus(required=True, status="approved")
    return state


def _pending_approval_requests(state: AgentState) -> list[ToolCallRequest]:
    plan = state["plan"] or {}
    approved_hashes = set(state["approved_tool_call_hashes"])
    return [
        request
        for raw_request in plan.get("requested_tools", [])
        for request in [
            raw_request
            if isinstance(raw_request, ToolCallRequest)
            else ToolCallRequest.model_validate(raw_request)
        ]
        if request.approval_required
        and tool_call_fingerprint(request) not in approved_hashes
    ]


def _approval_id(run_id: str, arguments_hash: str) -> str:
    digest = sha256(f"{run_id}:{arguments_hash}".encode()).hexdigest()
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


def _redact_arguments(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.lower() in SENSITIVE_ARGUMENT_KEYS:
        return "***"
    if isinstance(value, dict):
        return {
            item_key: _redact_arguments(item_value, key=item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_arguments(item) for item in value]
    return value
