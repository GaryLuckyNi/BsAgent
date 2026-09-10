import asyncio
import json
from typing import Any

from langchain_core.messages import AIMessage

from app.graph.planning.contracts import SubTaskModel, TaskResultModel
from app.graph.state import AgentState
from app.llm.factory import LlmClient


def build_analysis_agent_node(*, llm_client: LlmClient | None = None):
    def node(state: AgentState) -> dict[str, Any]:
        task = _active_analysis_task(state)
        if llm_client is None:
            return _terminal_failure(task, "analysis_llm_required", "分析模型未配置。")
        completion = asyncio.run(
            llm_client.complete(
                system_prompt=(
                    "你是投研任务执行器。只完成当前 task_goal，不生成最终答复。"
                    "只能基于用户原始问题与 dependencies 中已有事实推理；不得补造外部事实、"
                    "实时数据或来源。证据不足时明确说明缺口。输出简洁专业的中文分析。"
                ),
                user_prompt=json.dumps(
                    {
                        "user_message": state["user_message"],
                        "task_goal": task.task_goal,
                        "dependencies": _dependency_results(state, task),
                    },
                    ensure_ascii=False,
                ),
                temperature=0.1,
                max_tokens=1_024,
                thinking=False,
            )
        )
        content = completion.content.strip()
        if not content:
            return _terminal_failure(
                task,
                "analysis_output_empty",
                "分析模型未返回任务结果。",
            )
        return _success_update(state, task, content)

    return node


def _active_analysis_task(state: AgentState) -> SubTaskModel:
    task = SubTaskModel.model_validate(state.get("active_task"))
    if task.skill_name is not None or task.tool_name is not None:
        raise ValueError("Analysis Agent 只能执行无 Skill/Tool 的推理子任务。")
    return task


def _dependency_results(
    state: AgentState,
    task: SubTaskModel,
) -> list[dict[str, Any]]:
    dependency_ids = set(task.depends_on)
    return [
        dict(result)
        for result in state["task_results"]
        if result.get("task_id") in dependency_ids
    ]


def _success_update(
    state: AgentState,
    task: SubTaskModel,
    content: str,
) -> dict[str, Any]:
    result = TaskResultModel(task_id=task.id, status="success", content=content)
    return {
        "messages": [
            AIMessage(
                id=f"{state['request_id']}:task:{task.id}",
                content=content,
                additional_kwargs={"task_id": task.id, "task_role": "analysis"},
            )
        ],
        "task_results": [result.model_dump(mode="python")],
    }


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
    return {"task_results": [result.model_dump(mode="python")]}


analysis_agent_node = build_analysis_agent_node()
