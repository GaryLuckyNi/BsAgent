from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.tools.redaction import contains_inline_credentials


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=1, ge=1)
    backoff_ms: int = Field(default=0, ge=0)


class ToolSpec(BaseModel):
    name: str
    version: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    # 只供模型理解的有出处说明，不作为执行契约或授权。
    documentation: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=30_000, ge=1)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    permission_scope: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] = "low"
    # 宿主受信契约，不能由模型参数或工具返回声明；与审批风险等级独立。
    effect_type: Literal["read_only", "write", "unknown"] = "unknown"
    server_injected_fields: list[str] = Field(default_factory=list)


class ToolError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class ToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str | None = None
    tool_name: str
    tool_version: str | None = None
    arguments: dict[str, Any]
    purpose: str
    approval_required: bool = False

    @field_validator("arguments")
    @classmethod
    def reject_inline_credentials(cls, value: dict[str, Any]) -> dict[str, Any]:
        if contains_inline_credentials(value):
            raise PydanticCustomError(
                "inline_credentials_forbidden",
                "工具参数不得包含内联连接凭证，请由 Gateway 或 Adapter 注入。",
            )
        return value


class ToolCallResult(BaseModel):
    tool_call_id: str
    tool_name: str
    status: Literal["success", "failed", "timeout", "denied"]
    execution_state: Literal["not_started", "completed", "uncertain"] = "uncertain"
    result: JsonValue = None
    error: ToolError | None = None
    latency_ms: int = Field(default=0, ge=0)
    trace_id: str

    @model_validator(mode="before")
    @classmethod
    def legacy_execution_certainty(cls, value):
        if isinstance(value, dict) and "execution_state" not in value:
            return {**value, "execution_state": (
                "completed" if value.get("status") == "success" else "uncertain"
            )}
        return value


class ToolCallRecord(BaseModel):
    call_id: str | None = None
    request: ToolCallRequest
    result: ToolCallResult | None = None
