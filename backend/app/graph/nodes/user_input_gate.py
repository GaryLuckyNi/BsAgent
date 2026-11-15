from collections.abc import Callable
from typing import Literal

from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.graph.state import AgentState
from app.operations.langfuse_scheduler import record_recovery_event
from app.schemas.user_input import UserInputRequest, UserInputResponse


class _UserInputResume(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    interaction_id: str = Field(min_length=1, max_length=128)
    input_type: Literal["selection"]
    value: str = Field(min_length=1, max_length=256)
    presentation_version: str | None = Field(default=None, max_length=128)
    batch_digest: str | None = Field(default=None, max_length=64)

    @field_validator("interaction_id", "value")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("user input resume text must not be blank")
        return normalized


class UserInputResumeError(ValueError):
    http_status = 409

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


def user_input_gate_node(
    state: AgentState, *,
    accept_recovery: Callable[[AgentState, UserInputRequest, str], None] | None = None,
) -> dict:
    request = state.get("pending_user_input")
    if request is None:
        return {}

    payload = {
            "interrupt_type": "user_input",
            "interaction_id": request.interaction_id,
            "input_type": request.input_type,
            "question": request.question,
            "options": [option.model_dump(mode="json") for option in request.options],
        }
    if request.recovery_batch is not None:
        payload["recovery_batch"] = request.recovery_batch.model_dump(mode="json")
    resume_value = interrupt(payload)
    try:
        resume = _UserInputResume.model_validate(resume_value)
    except ValidationError as exc:
        raise UserInputResumeError(
            "用户输入恢复载荷与中断请求不匹配。",
            code="user_input_resume_mismatch",
        ) from exc

    if (
        resume.interaction_id != request.interaction_id
        or resume.input_type != request.input_type
    ):
        raise UserInputResumeError(
            "用户输入恢复目标与中断请求不匹配。",
            code="user_input_resume_mismatch",
        )
    allowed_values = {option.value for option in request.options}
    if resume.value not in allowed_values:
        raise UserInputResumeError(
            "用户选择不在服务端发布的候选项中。",
            code="user_input_selection_invalid",
        )

    if request.recovery_batch is not None:
        expected = request.recovery_batch
        if resume.presentation_version != expected.version or resume.batch_digest != expected.digest:
            record_recovery_event("recovery_invalidated", request_id=state.get("request_id"), batch_id=expected.batch_id,
                route_reason="permission_recovery_presentation_mismatch")
            raise UserInputResumeError(
                "恢复任务清单或显示协议不匹配，请刷新后重新确认。",
                code="permission_recovery_presentation_mismatch",
            )
        if accept_recovery is None:
            raise UserInputResumeError(
                "当前执行上下文不支持批次恢复，请重新发起请求。",
                code="permission_recovery_context_required",
            )
        # 回调必须先校验服务端快照并持久化决定；等待节点不执行外部工具。
        accept_recovery(state, request, resume.value)

    return {
        "pending_user_input": None,
        "user_input_response": UserInputResponse(
            interaction_id=request.interaction_id,
            input_type=request.input_type,
            value=resume.value,
            source=request.source,
            presentation_version=resume.presentation_version,
            batch_digest=resume.batch_digest,
        ),
    }
