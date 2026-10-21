from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
        return self


class UserInputResponse(_StrictFrozenModel):
    interaction_id: str = Field(min_length=1, max_length=128)
    input_type: Literal["selection"]
    value: str = Field(min_length=1, max_length=256)
    source: UserInputSource

    @field_validator("interaction_id", "value")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("user input response text must not be blank")
        return normalized
