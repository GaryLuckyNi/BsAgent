"""为规划和执行提供明确的证据引用与有预算的用户材料。"""

import json

from langchain_core.messages import AIMessage, ToolMessage

from app.graph.planning.contracts import SubTaskModel


def task_evidence(state: dict, task: SubTaskModel) -> list[dict]:
    dependency_ids = set(task.depends_on)
    reference_ids = set(task.evidence_task_ids)
    return [
        dict(result)
        for result in state.get("task_results", [])
        if result.get("task_id") in dependency_ids
        or (result.get("task_id") in reference_ids and result.get("status") == "success")
    ]


def user_materials(state: dict, *, limit: int = 16000) -> list[dict]:
    candidates = []
    for message in state.get("messages", [])[-12:]:
        if isinstance(message, ToolMessage) or (
            isinstance(message, AIMessage)
            and (message.tool_calls or message.additional_kwargs.get("task_role"))
        ):
            continue
        candidates.append({"kind": message.type, "text": str(message.content)})
    for item in state.get("image_analyses", []):
        candidates.append({"kind": "attachment", "text": json.dumps(item, ensure_ascii=False)})
    materials = []
    for item in reversed(candidates):
        if limit <= 0:
            break
        text = item["text"][: min(limit, 6000)]
        materials.append(
            {"kind": item["kind"], "text": text, "truncated": len(text) < len(item["text"])}
        )
        limit -= len(text)
    return list(reversed(materials))
