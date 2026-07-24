from typing import Any, Literal

from pydantic import BaseModel, Field


class ApprovalRequest(BaseModel):
    approval_id: str
    run_id: str
    approval_type: Literal["answer_release", "tool_call", "memory_write"]
    risk_level: Literal["low", "medium", "high"]
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "approved", "rejected"] = "pending"


class ApprovalStatus(BaseModel):
    required: bool
    status: Literal["not_required", "pending", "approved", "rejected"]


class PendingToolApproval(BaseModel):
    call_id: str
    step_index: int = Field(ge=0)
    request_fingerprint: str
    tool_name: str
    tool_version: str | None = None
    risk_level: Literal["low", "medium", "high"]
    safe_arguments: dict[str, Any] = Field(default_factory=dict)
    purpose: str
