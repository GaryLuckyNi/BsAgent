"""Skill 固定工作流的 checkpoint 契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkflowError(ValueError):
    def __init__(self, message: str, code: str = "skill_workflow_invalid") -> None:
        super().__init__(message)
        self.code = code


class StepDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=1, max_length=500)
    source: str = Field(min_length=1, max_length=6000)
    tool_name: str | None = Field(default=None, min_length=1, max_length=256)
    required_fields: list[str] = Field(default_factory=list, max_length=40)
    completion_criteria: list[str] = Field(default_factory=list, max_length=8)
    missing_policy: Literal["stop", "continue"] = "stop"
    missing_policy_source: str | None = None


class WorkflowDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[StepDraft] = Field(min_length=1, max_length=12)
    output_requirement: str = Field(default="", max_length=4000)


class WorkflowStep(StepDraft):
    task_id: str
    tool_version: str | None = None


class SkillWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_task_id: str
    skill_name: str
    skill_version: str
    definition_hash: str
    summary: str
    output_requirement: str
    steps: list[WorkflowStep]
    checked_steps: list[str] = Field(default_factory=list)
    status: Literal["running", "completed", "failed"] = "running"
