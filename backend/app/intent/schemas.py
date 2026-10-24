from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BusinessIntentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    intent_type: str = Field(min_length=1, max_length=64)
    domain: str = Field(min_length=1, max_length=128)
    subdomain: str = Field(min_length=1, max_length=128)
    trigger_keywords: list[str] = Field(default_factory=list)
    trigger_examples: list[str] = Field(default_factory=list)
    description: str | None = None


class IntentDecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_type: str | None = None
    domain: str | None = None
    subdomain: str | None = None
    goal: str | None = None
    confidence: float = Field(ge=0, le=1)
    source_constraint: Literal["unrestricted", "original_only", "unclear"] = "unclear"

    @model_validator(mode="after")
    def validate_scope_shape(self) -> "IntentDecisionModel":
        taxonomy = (self.intent_type, self.domain, self.subdomain)
        if self.intent_type is None:
            if any(value is not None for value in taxonomy[1:]) or self.goal is not None:
                raise ValueError("out-of-scope intent must not include taxonomy or goal")
            return self
        if any(not value for value in (*taxonomy, self.goal)):
            raise ValueError("in-scope intent requires taxonomy and goal")
        return self
