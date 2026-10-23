from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_PLAN_TASKS = 12
MAX_TASK_RESULT_CONTENT_CHARS = 8_000


class SubTaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    task_goal: str = Field(min_length=1, max_length=500)
    execution_type: Literal["tool", "analysis"] = "tool"
    completion_criteria: list[str] = Field(default_factory=list, max_length=8)
    tool_search_query: str | None = Field(default=None, min_length=1, max_length=500)
    evidence_task_ids: list[str] = Field(default_factory=list, max_length=24)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    skill_name: str | None = None

    @model_validator(mode="after")
    def validate_execution_capability(self):
        if self.execution_type == "analysis" and self.skill_name is not None:
            raise ValueError("纯分析任务不能同时绑定 Skill。")
        return self

    @field_validator("task_goal", "tool_search_query", "skill_name", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("depends_on", "evidence_task_ids")
    @classmethod
    def reject_duplicate_dependencies(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("任务依赖不能重复。")
        return value


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_summary: str | None = Field(default=None, min_length=1, max_length=300)
    tasks: list[SubTaskModel] = Field(min_length=1, max_length=MAX_PLAN_TASKS)

    @model_validator(mode="after")
    def validate_task_graph(self):
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("计划内任务 ID 必须唯一。")

        known_ids = set(task_ids)
        dependencies = {task.id: set(task.depends_on) for task in self.tasks}
        for task_id, task_dependencies in dependencies.items():
            unknown = task_dependencies - known_ids
            if unknown:
                raise ValueError(f"任务 {task_id} 引用了未知依赖。")
            if task_id in task_dependencies:
                raise ValueError(f"任务 {task_id} 不能依赖自身。")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("任务计划不能包含循环依赖。")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency_id in dependencies[task_id]:
                visit(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        return self


class TaskResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: Literal["success", "business_failure", "terminal_failure"]
    content: str = Field(max_length=MAX_TASK_RESULT_CONTENT_CHARS)
    error_code: str | None = None
    tool_name: str | None = None


class PlannerFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["continue", "revise", "finish"]
    reason: str = Field(min_length=1, max_length=800)
    plan: PlanModel | None = None
    unresolved_questions: list[str] = Field(default_factory=list, max_length=12)
    synthesis_task_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_action(self):
        if (self.action == "revise") != (self.plan is not None):
            raise ValueError("仅 revise 必须携带 plan；continue 和 finish 不得携带计划。")
        if self.synthesis_task_id is not None and self.action != "finish":
            raise ValueError("只有 finish 可以将终端分析任务交给最终回答节点。")
        return self
