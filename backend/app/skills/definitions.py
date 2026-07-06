from typing import Any, Literal

from pydantic import BaseModel, Field


class SkillCard(BaseModel):
    name: str
    version: str
    summary: str
    supported_intents: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"]
    definition_hash: str


class SkillDefinition(BaseModel):
    name: str
    version: str
    description: str
    input_contract: dict[str, Any] = Field(default_factory=dict)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    definition_hash: str


class SkillExecutionGuide(BaseModel):
    name: str
    version: str
    instructions: list[str] = Field(default_factory=list)
    few_shot_examples: list[dict[str, Any]] = Field(default_factory=list)
    recovery_policy: list[str] = Field(default_factory=list)
    required_evidence_policy: str | None = None
    definition_hash: str
