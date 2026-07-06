from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


IntentKind = Literal[
    "knowledge_qa",
    "memory_query",
    "tool_action",
    "task_workflow",
    "conversation",
    "clarification_needed",
    "unsupported",
]
RiskLevel = Literal["low", "medium", "high"]


class BusinessIntentCreate(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    intent: IntentKind
    domain: str = Field(min_length=1, max_length=128)
    sub_intent: str = Field(min_length=1, max_length=128)
    candidate_skill: str | None = Field(default=None, max_length=128)
    risk_level: RiskLevel = "medium"
    trigger_keywords: list[str] = Field(default_factory=list)
    trigger_examples: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    enabled: bool = True
    description: str | None = None


class BusinessIntentUpdate(BaseModel):
    intent: IntentKind | None = None
    domain: str | None = Field(default=None, min_length=1, max_length=128)
    sub_intent: str | None = Field(default=None, min_length=1, max_length=128)
    candidate_skill: str | None = Field(default=None, max_length=128)
    risk_level: RiskLevel | None = None
    trigger_keywords: list[str] | None = None
    trigger_examples: list[str] | None = None
    required_capabilities: list[str] | None = None
    enabled: bool | None = None
    description: str | None = None


class BusinessIntentRead(BusinessIntentCreate):
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(default="1.0.0", min_length=1, max_length=64)
    summary: str = Field(min_length=1)
    supported_intents: list[str] = Field(default_factory=list)
    supported_domains: list[str] = Field(default_factory=list)
    supported_sub_intents: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = "medium"
    enabled: bool = True


class SkillUpdate(BaseModel):
    version: str | None = Field(default=None, min_length=1, max_length=64)
    summary: str | None = Field(default=None, min_length=1)
    supported_intents: list[str] | None = None
    supported_domains: list[str] | None = None
    supported_sub_intents: list[str] | None = None
    allowed_tools: list[str] | None = None
    risk_level: RiskLevel | None = None
    enabled: bool | None = None


class SkillRead(SkillCreate):
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class Page(BaseModel):
    items: list
    total: int
