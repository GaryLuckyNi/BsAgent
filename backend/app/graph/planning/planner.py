import asyncio
from dataclasses import dataclass
import json

from pydantic import ValidationError

from app.graph.planning.contracts import PlanModel, TaskResultModel
from app.graph.planning.json_output import extract_json_object
from app.llm.factory import LlmClient
from app.skills.definitions import SkillCard
from app.tools.search import ToolSearchHit


class PlannerOutputError(RuntimeError):
    code = "planner_output_invalid"


class UndisclosedPlannerCapabilityError(PlannerOutputError):
    code = "planner_capability_not_disclosed"


@dataclass(frozen=True)
class PlannerCapabilities:
    skills: tuple[SkillCard, ...] = ()
    tools: tuple[ToolSearchHit, ...] = ()


class LlmTaskPlanner:
    def __init__(self, *, llm_client: LlmClient) -> None:
        self._llm_client = llm_client

    def plan(
        self,
        *,
        goal: str,
        intent_type: str,
        domain: str,
        subdomain: str,
        capabilities: PlannerCapabilities,
        task_results: tuple[TaskResultModel, ...],
        remaining_iterations: int,
        planning_constraints: dict | None = None,
    ) -> PlanModel:
        result = asyncio.run(
            self._llm_client.complete(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=json.dumps(
                    {
                        "goal": goal,
                        "intent_type": intent_type,
                        "domain": domain,
                        "subdomain": subdomain,
                        "remaining_iterations": remaining_iterations,
                        "planning_constraints": planning_constraints or {},
                        "skills": [
                            {"name": card.name, "summary": card.summary}
                            for card in capabilities.skills
                        ],
                        "tools": [
                            {
                                "name": hit.tool_name,
                                "description": hit.description,
                                "required_arguments": hit.required_arguments,
                            }
                            for hit in capabilities.tools
                        ],
                        "observations": [
                            result.model_dump(mode="json") for result in task_results
                        ],
                        "required_schema": {
                            "tasks": [
                                {
                                    "id": "lower_snake_case string",
                                    "task_goal": "string",
                                    "depends_on": ["task_id"],
                                    "skill_name": "disclosed skill name|null",
                                    "tool_name": "disclosed tool name|null",
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
                temperature=0.0,
                max_tokens=2_048,
                thinking=False,
            )
        )
        try:
            plan = PlanModel.model_validate(extract_json_object(result.content))
        except (ValueError, ValidationError) as exc:
            raise PlannerOutputError("Planner 返回了无效任务计划。") from exc
        _validate_disclosed_capabilities(plan, capabilities=capabilities)
        return plan


def _validate_disclosed_capabilities(
    plan: PlanModel,
    *,
    capabilities: PlannerCapabilities,
) -> None:
    skill_names = {card.name for card in capabilities.skills}
    tool_names = {hit.tool_name for hit in capabilities.tools}
    for task in plan.tasks:
        if task.skill_name is not None and task.skill_name not in skill_names:
            raise UndisclosedPlannerCapabilityError(
                f"Planner 选择了未披露 Skill: {task.skill_name}"
            )
        if task.tool_name is not None and task.tool_name not in tool_names:
            raise UndisclosedPlannerCapabilityError(
                f"Planner 选择了未披露 Tool: {task.tool_name}"
            )


_SYSTEM_PROMPT = """
你是 BsAgent 的投研任务 Planner。只输出单行 JSON object，不要输出 Markdown。
根据 goal 拆分完成目标所必需的最少任务，最多 12 个。任务可以选择一个已披露 Skill、
一个已披露 Tool，或两者都不选以执行基于已有上下文的分析；同一任务不能同时选择
Skill 和 Tool。depends_on 表示真实数据依赖，无依赖任务应保持可并行。不要预先生成
工具参数，不要猜测未披露能力，不要添加用户未要求的固定分析维度。observations 包含
此前执行结果或业务错误；需要修正路径时据此生成新计划。任务 ID 使用 lower_snake_case。
""".strip()
