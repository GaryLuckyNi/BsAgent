import json
from typing import Any

from langchain_core.messages import AIMessage

from app.graph.planning.context import task_evidence, user_materials
from app.graph.planning.contracts import SubTaskModel, TaskResultModel
from app.graph.public_events import build_public_llm_stream_handler
from app.graph.state import AgentState
from app.llm.factory import LlmClient, invoke_llm_completion
from app.skills.registry import SkillRegistry


def build_skill_agent_node(
    *,
    registry: SkillRegistry,
    llm_client: LlmClient | None = None,
):
    def node(state: AgentState) -> dict[str, Any]:
        task = _active_skill_task(state)
        if llm_client is None:
            return _terminal_failure(task, "skill_llm_required", "Skill 执行模型未配置。")
        definition, guide = registry.get_definition_layers(name=task.skill_name or "")
        completion = invoke_llm_completion(
            llm_client,
            system_prompt=(
                "你是投研 Skill 执行器。严格按照加载的 Skill 指南完成当前 task_goal，"
                "不要生成最终回答。只使用 dependencies 中已有事实；不得假装已调用工具，"
                "不得补造数据。缺少必要输入时明确说明。输出简洁专业的中文任务结果。"
            ),
            user_prompt=json.dumps(
                {
                    "user_message": state["user_message"],
                    "task_goal": task.task_goal,
                    "completion_criteria": task.completion_criteria,
                    "user_materials": user_materials(state),
                    "skill": {
                        "name": definition.name,
                        "description": definition.description,
                        "constraints": definition.constraints,
                        "output_contract": definition.output_contract,
                    },
                    "instructions": guide.instructions,
                    "recovery_policy": guide.recovery_policy,
                    "dependencies": _dependency_results(state, task),
                },
                ensure_ascii=False,
            ),
            temperature=0.1,
            max_tokens=2_048,
            thinking=True,
            on_delta=build_public_llm_stream_handler("skill_agent", state),
            retry_without_thinking_on_empty=True,
        )
        content = completion.content.strip()
        if not content:
            return _terminal_failure(
                task,
                "skill_output_empty",
                "Skill 执行模型未返回任务结果。",
            )
        result = TaskResultModel(task_id=task.id, status="success", content=content)
        return {
            "messages": [
                AIMessage(
                    id=f"{state['request_id']}:task:{task.id}",
                    content=content,
                    additional_kwargs={"task_id": task.id, "task_role": "skill"},
                )
            ],
            "task_results": [result.model_dump(mode="python", exclude={"tool_name"})],
        }

    return node


def _active_skill_task(state: AgentState) -> SubTaskModel:
    task = SubTaskModel.model_validate(state.get("active_task"))
    if task.skill_name is None:
        raise ValueError("Skill Agent 只能执行包含 skill_name 的子任务。")
    return task


def _dependency_results(
    state: AgentState,
    task: SubTaskModel,
) -> list[dict[str, Any]]:
    return task_evidence(state, task)


def _terminal_failure(
    task: SubTaskModel,
    code: str,
    content: str,
) -> dict[str, Any]:
    result = TaskResultModel(
        task_id=task.id,
        status="terminal_failure",
        content=content,
        error_code=code,
    )
    return {"task_results": [result.model_dump(mode="python", exclude={"tool_name"})]}
