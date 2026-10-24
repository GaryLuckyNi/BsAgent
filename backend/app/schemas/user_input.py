from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.recovery import RECOVERY_KIND, RECOVERY_VERSION, RecoveryBatchView


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UserInputOption(_StrictFrozenModel):
    value: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=240)

    @field_validator("value", "label", "description")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("user input option text must not be blank")
        return normalized


class UserInputSource(_StrictFrozenModel):
    node: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=128)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    call_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("node", "kind", "task_id", "call_id")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("user input source text must not be blank")
        return normalized


class UserInputRequest(_StrictFrozenModel):
    interaction_id: str = Field(min_length=1, max_length=128)
    input_type: Literal["selection"]
    question: str = Field(min_length=1, max_length=500)
    options: tuple[UserInputOption, ...] = Field(min_length=2, max_length=10)
    source: UserInputSource
    recovery_batch: RecoveryBatchView | None = None

    @field_validator("interaction_id", "question")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("user input request text must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_unique_option_values(self) -> "UserInputRequest":
        values = [option.value for option in self.options]
        if len(values) != len(set(values)):
            raise ValueError("user input option values must be unique")
        if (self.source.kind == RECOVERY_KIND) != (self.recovery_batch is not None):
            raise ValueError("permission recovery requires its complete batch view")
        if self.recovery_batch is not None and (
            self.source.node != "task_scheduler" or self.source.task_id is not None
            or self.source.call_id is not None or set(values) != {"web_search", "use_existing"}
        ):
            raise ValueError("permission recovery must use batch source and fixed choices")
        return self


class UserInputResponse(_StrictFrozenModel):
    interaction_id: str = Field(min_length=1, max_length=128)
    input_type: Literal["selection"]
    value: str = Field(min_length=1, max_length=256)
    source: UserInputSource
    presentation_version: str | None = Field(default=None, max_length=128)
    batch_digest: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def require_batch_presentation(self):
        if self.source.kind == RECOVERY_KIND and (
            self.presentation_version != RECOVERY_VERSION or self.batch_digest is None
            or len(self.batch_digest) != 64
        ):
            raise ValueError("permission recovery presentation missing")
        return self

    @field_validator("interaction_id", "value")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("user input response text must not be blank")
        return normalized
