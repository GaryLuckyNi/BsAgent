from typing import Any, Literal

from pydantic import BaseModel, Field


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=1, ge=1)
    backoff_ms: int = Field(default=0, ge=0)


class ToolSpec(BaseModel):
    name: str
    version: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=30_000, ge=1)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    permission_scope: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] = "low"


class ToolError(BaseModel):
    code: str
    message: str


class ToolCallRequest(BaseModel):
    tool_name: str
    tool_version: str | None = None
    arguments: dict[str, Any]
    purpose: str
    approval_required: bool = False


class ToolCallResult(BaseModel):
    tool_call_id: str
    tool_name: str
    status: Literal["success", "failed", "timeout", "denied"]
    result: dict[str, Any] | None = None
    error: ToolError | None = None
    latency_ms: int = Field(default=0, ge=0)
    trace_id: str


class ToolCallRecord(BaseModel):
    request: ToolCallRequest
    result: ToolCallResult | None = None
