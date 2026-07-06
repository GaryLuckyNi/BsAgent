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
