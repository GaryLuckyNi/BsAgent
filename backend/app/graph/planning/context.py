"""为规划和执行提供明确的证据引用与有预算的用户材料。"""

import json

from langchain_core.messages import AIMessage, ToolMessage

from app.graph.planning.contracts import SubTaskModel
from app.graph.state import context_compaction_payload, is_context_compaction_message


def task_evidence(state: dict, task: SubTaskModel) -> list[dict]:
    dependency_ids = set(task.depends_on)
    reference_ids = set(task.evidence_task_ids)
    return [
        dict(result)
        for result in state.get("task_results", [])
        if result.get("task_id") in dependency_ids
        or (result.get("task_id") in reference_ids and result.get("status") == "success")
    ]


def user_materials(
    state: dict, *, limit: int = 16000, include_current_message: bool = True,
) -> list[dict]:
    materials: list[dict] = []
    summary_message = next(
        (
            message
            for message in state.get("messages", [])
            if is_context_compaction_message(message)
        ),
        None,
    )
    if summary_message is not None and limit > 0:
        payload = context_compaction_payload(summary_message)
        if payload is not None:
            summary_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            text = summary_text[: min(limit, 12000)]
            materials.append(
                {
                    "ref": f"message:{summary_message.id}",
                    "kind": "context_summary",
                    "text": text,
                    "truncated": len(text) < len(summary_text),
                }
            )
            limit -= len(text)

    candidates = []
    for message in state.get("messages", [])[-12:]:
        if not include_current_message and message.additional_kwargs.get("metadata", {}).get(
            "request_id"
        ) == state.get("request_id") and message.type == "human":
            continue
        if is_context_compaction_message(message) or isinstance(message, ToolMessage) or (
            isinstance(message, AIMessage)
            and (message.tool_calls or message.additional_kwargs.get("task_role"))
        ):
            continue
        candidates.append({"ref": f"message:{message.id}", "kind": message.type, "text": str(message.content)})
    for item in state.get("image_analyses", []):
        candidates.append({"ref": f"attachment:{item.get('asset_id')}", "kind": "attachment", "text": json.dumps(item, ensure_ascii=False)})
    for item in reversed(candidates):
        if limit <= 0:
            break
        text = item["text"][: min(limit, 6000)]
        materials.append(
            {"ref": item["ref"], "kind": item["kind"], "text": text, "truncated": len(text) < len(item["text"])}
        )
        limit -= len(text)
    return materials


def request_context(state: dict) -> dict:
    """业务信息不是核实证明；引用只来自本次可访问材料。"""
    return {
        "available_information": (state.get("intent") or {}).get("available_information") or {},
        "materials": user_materials(state, include_current_message=False),
    }


def result_catalog(state: dict, *, limit: int = 8000) -> list[dict]:
    """给入口提供有界结果目录，不反查历史原始记录，不复制 artifact。"""
    output = []
    for result in state.get("task_results", [])[-24:]:
        text = str(result.get("content") or "")
        excerpt = text[:min(limit, 1000)]
        if not excerpt:
            continue
        output.append({
            "ref": f"task:{result['task_id']}", "status": result["status"],
            "text": excerpt, "truncated": len(excerpt) < len(text),
        })
        limit -= len(excerpt)
        if limit <= 0:
            break
    return output
