from typing import Literal

from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.graph.state import AgentState
from app.schemas.user_input import UserInputResponse


class _UserInputResume(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    interaction_id: str = Field(min_length=1, max_length=128)
    input_type: Literal["selection"]
    value: str = Field(min_length=1, max_length=256)

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


def user_input_gate_node(state: AgentState) -> dict:
    request = state.get("pending_user_input")
    if request is None:
        return {}

    resume_value = interrupt(
        {
            "interrupt_type": "user_input",
            "interaction_id": request.interaction_id,
            "input_type": request.input_type,
            "question": request.question,
            "options": [option.model_dump(mode="json") for option in request.options],
        }
    )
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

    return {
        "pending_user_input": None,
        "user_input_response": UserInputResponse(
            interaction_id=request.interaction_id,
            input_type=request.input_type,
            value=resume.value,
            source=request.source,
        ),
    }
