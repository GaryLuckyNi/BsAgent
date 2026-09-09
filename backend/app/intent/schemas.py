from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


class TaskEntityModel(BaseModel):
    type: Literal["company", "industry", "security", "document"]
    mention: str = Field(min_length=1, max_length=128)


class TaskBriefModel(BaseModel):
    user_goal: str = Field(min_length=1, max_length=500)
    entities: list[TaskEntityModel] = Field(default_factory=list, max_length=10)
    analysis_questions: list[str] = Field(default_factory=list, max_length=12)
    search_keywords: list[str] = Field(default_factory=list, max_length=12)
    freshness_required: bool = False
    web_search_required: bool = False
    web_search_reason: str | None = Field(default=None, max_length=240)
    rag_required: bool = False
    time_horizon: str | None = Field(default=None, max_length=120)
    output_expectation: str = Field(max_length=500)
    constraints: list[str] = Field(default_factory=list, max_length=12)
    needs_clarification: bool = False


class IntentRoutingResult(BaseModel):
    decision: IntentDecisionModel
    task_brief: TaskBriefModel | None = None

    @model_validator(mode="after")
    def require_task_brief_for_workflow(self) -> "IntentRoutingResult":
        if self.decision.intent == "task_workflow" and self.task_brief is None:
            raise ValueError("task_workflow routing requires task_brief")
        return self
