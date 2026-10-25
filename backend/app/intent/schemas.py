import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


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
    source_constraint: Literal["unrestricted", "original_only", "confirmation_required", "unclear"] = "unclear"
    available_information: dict[str, JsonValue] = Field(default_factory=dict)
    route: Literal["direct_answer", "synthesize", "plan"] = "plan"
    answer: str | None = None

    @field_validator("available_information")
    @classmethod
    def validate_json_information(cls, value):
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value

    @model_validator(mode="after")
    def validate_response_branch(self) -> "IntentDecisionModel":
        if self.intent_type is None:
            if self.answer is not None or self.available_information or self.route != "plan":
                raise ValueError("范围外请求不携带回答或业务信息")
        elif self.route == "direct_answer":
            if not self.answer or not self.answer.strip():
                raise ValueError("直接回答必须提供非空 answer")
        elif self.answer is not None:
            raise ValueError("执行或合成分支不能提前输出答案")
        return self

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
