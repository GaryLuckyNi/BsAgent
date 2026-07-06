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


class BusinessIntentProfile(BaseModel):
    id: str
    intent: IntentKind
    domain: str
    sub_intent: str
    candidate_skill: str | None = None
    candidate_tool: str | None = None
    risk_level: RiskLevel = "medium"
    trigger_keywords: list[str] = Field(default_factory=list)
    trigger_examples: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)


class IntentSignal(BaseModel):
    risk_floor: RiskLevel = "medium"
    hard_intent: IntentKind | None = None
    needs_clarification: bool = False
    reason_codes: list[str] = Field(default_factory=list)


class IntentDecisionModel(BaseModel):
    intent: IntentKind
    domain: str | None = None
    sub_intent: str | None = None
    candidate_skill: str | None = None
    candidate_tool: str | None = None
    risk_level: RiskLevel = "medium"
    confidence: float = Field(ge=0, le=1)
    needs_clarification: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    next_node: str = "planner_agent"
