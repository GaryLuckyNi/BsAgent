"""固定 Skill 子步骤的调度；不修改外层 Planner 的计划。"""

from langgraph.types import Send

from app.graph.planning.contracts import SubTaskModel
from app.skills.workflow_contracts import SkillWorkflow


def active_step(state):
    reference = state.get("active_skill_step")
    if not reference:
        return None
    workflow = SkillWorkflow.model_validate(state["skill_workflows"][reference["parent_task_id"]])
    step = workflow.steps[reference["index"]]
    if step.task_id != state["active_task"]["id"] or workflow.status != "running":
        raise ValueError("Skill 步骤与运行快照不一致。")
    return workflow, step


def dispatch_workflow(state):
    results = {item["task_id"]: item for item in state.get("task_results", [])}
    parents = {item["id"]: item for item in (state.get("plan") or {}).get("tasks", [])}
    for parent_id, value in state.get("skill_workflows", {}).items():
        workflow = SkillWorkflow.model_validate(value)
        if workflow.status != "running" or parent_id not in parents:
            continue
        parent = SubTaskModel.model_validate(parents[parent_id])
        previous = []
        for index, step in enumerate(workflow.steps):
            result = results.get(step.task_id)
            if result is not None and result["status"] != "success":
                break
            if result is not None and step.task_id in workflow.checked_steps:
                previous.append(step.task_id)
                continue
            if step.tool_name is None and result is None:
                # 保留旧步骤契约，但由 Skill 节点明确结算不支持的步骤，不能转成工具调用。
                return [Send("skill_agent", {
                    **state, "active_task": parent.model_dump(), "active_skill_step": None,
                })]
            child = SubTaskModel(
                id=step.task_id,
                task_goal=step.instruction,
                execution_type="tool",
                completion_criteria=step.completion_criteria,
                depends_on=list(dict.fromkeys([*parent.depends_on, *previous])),
                evidence_task_ids=parent.evidence_task_ids,
            )
            payload = {
                **state,
                "active_task": child.model_dump(),
                "active_skill_step": {"parent_task_id": parent_id, "index": index},
            }
            worker = (
                "skill_step_check"
                if result
                else "tool_agent"
            )
            return [Send(worker, payload)]
        return [
            Send(
                "skill_agent",
                {**state, "active_task": parent.model_dump(), "active_skill_step": None},
            )
        ]
    return None


def workflow_failed(state):
    return any(item.get("status") == "failed" for item in state.get("skill_workflows", {}).values())
