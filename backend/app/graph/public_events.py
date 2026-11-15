import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urldefrag, urlsplit

from langgraph.config import get_stream_writer

from app.graph.recovery.search import recovery_failure_message
from app.llm.schemas import LlmStreamDelta
from app.security.public_output import public_progress, public_sources, public_text, public_web_url
from app.tools.redaction import sanitize_untrusted_details

_STAGE_LABELS = {
    "intent_router": "理解问题",
    "planner_agent": "制定计划",
    "skill_agent": "执行研究流程",
    "tool_agent": "获取证据",
    "analysis_agent": "分析证据",
    "synthesizer": "生成回答",
}
_PUBLIC_CONTENT_BATCH_CHARS = 24


def emit_answer_sources(sources: list[dict]) -> None:
    sources = public_sources(sources)
    if not sources:
        return
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer({"type": "answer_sources", "sources": sources})


def emit_public_progress(
    node_name: str,
    state: dict[str, Any],
    *,
    status: str,
) -> None:
    label = _STAGE_LABELS.get(node_name)
    if label is None:
        return
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    if (node_name == "synthesizer" and status == "completed"
            and state.get("draft_answer") and not state.get("pending_replan")):
        for task in (state.get("plan") or {}).get("tasks", []):
            for task_state in _failed_task_states(state, task):
                emit_public_progress(_task_stage(task_state["active_task"]), task_state, status="failed")
    kind, message = _public_event_content(node_name, state, status=status)
    public_status = _public_status(node_name, state, status=status)
    event = {
        "type": "progress",
        "kind": kind,
        "id": _progress_id(node_name, state),
        "stage": node_name,
        "label": {"goal": "研究目标", "plan": "研究计划"}.get(kind, label),
        "status": public_status,
        "message": message,
    }
    event.update(_tool_activity_metadata(node_name, state, status=public_status))
    writer(public_progress(event))


def _task_stage(task: dict) -> str:
    return "skill_agent" if task.get("skill_name") else "tool_agent"


def _failed_task_states(state: dict, task: dict):
    task_state = {**state, "active_task": task}
    result = _matching_task_result(task_state)
    if not result or result.get("status") == "success":
        return
    yield task_state
    if task.get("skill_name"):
        workflow = (state.get("skill_workflows") or {}).get(task["id"], {})
        for step in workflow.get("steps", []):
            child = {"id": step["task_id"], "task_goal": task["task_goal"], "tool_name": step.get("tool_name")}
            child_state = {**state, "active_task": child}
            child_result = _matching_task_result(child_state)
            if child_result and child_result.get("status") != "success":
                yield child_state


def emit_replanned_task_progress(state: dict, *, previous_plan: dict, previous_iteration: int) -> None:
    """新计划确认替代失败路径后，覆盖原活动；不把旧执行结果改成成功。"""
    active_ids = {task["id"] for task in (state.get("plan") or {}).get("tasks", [])}
    for task in previous_plan.get("tasks", []):
        if task["id"] in active_ids:
            continue
        for task_state in _failed_task_states({**state, "planner_iteration": previous_iteration}, task):
            emit_public_progress(_task_stage(task_state["active_task"]), task_state, status="superseded")


def build_public_llm_stream_handler(
    node_name: str,
    state: dict[str, Any],
    *,
    stream_content: bool = False,
) -> "PublicLlmStreamHandler":
    del node_name, state
    try:
        writer = get_stream_writer()
    except RuntimeError:
        writer = None
    return PublicLlmStreamHandler(
        writer=writer,
        stream_content=stream_content,
    )


def build_public_field_stream(node_name: str, state: dict[str, Any], *, feedback: bool = False):
    from app.graph.planning.progress_stream import ProgressFieldStream

    try:
        writer = get_stream_writer()
    except RuntimeError:
        return None
    kind = "goal" if node_name == "intent_router" else "plan"
    event_id = _progress_id(node_name, state)

    def publish(text: str, *, status: str = "started") -> None:
        if writer is not None:
            writer({
                "type": "progress", "kind": kind, "id": event_id, "stage": node_name,
                "label": "研究目标" if kind == "goal" else "研究计划",
                "status": status, "message": public_text(text),
            })

    return ProgressFieldStream(
        kind=kind, on_text=publish, feedback=feedback,
        clean=lambda text: _safe_task_goal(text, max_chars=None),
        on_failed=lambda: publish(
            "研究目标未能确定。" if kind == "goal" else "研究计划未能完成。", status="failed",
        ),
    )


class PublicLlmStreamHandler:
    def __init__(
        self,
        *,
        writer: Callable[[dict[str, Any]], None] | None,
        stream_content: bool,
    ) -> None:
        self._writer = writer
        self._stream_content = stream_content
        self._content_buffer = ""
        self.content_started = False

    def __call__(self, event: LlmStreamDelta) -> None:
        if self._writer is None:
            return
        if event.kind == "content" and self._stream_content:
            self._content_buffer += event.delta
            if (
                len(self._content_buffer) >= _PUBLIC_CONTENT_BATCH_CHARS
                or any(mark in event.delta for mark in "\n。！？")
            ):
                self._flush_content()

    def flush(self) -> None:
        self._flush_content()

    def _flush_content(self) -> None:
        if not self._content_buffer or self._writer is None:
            return
        content = self._content_buffer
        self._content_buffer = ""
        self._writer({"type": "answer_delta", "content": content})
        self.content_started = True

def _progress_id(node_name: str, state: dict[str, Any]) -> str:
    active_task = state.get("active_task") or {}
    if active_task.get("id"):
        return f"{node_name}:{active_task['id']}"
    if node_name == "planner_agent":
        return f"{node_name}:{state.get('planner_iteration', 0)}"
    return node_name


def _public_event_content(
    node_name: str,
    state: dict[str, Any],
    *,
    status: str,
) -> tuple[str, str]:
    if status == "completed" and node_name == "intent_router":
        goal = (state.get("intent") or {}).get("goal")
        if goal:
            return "goal", _safe_task_goal(goal, max_chars=None)
    if status == "completed" and node_name == "planner_agent":
        tasks = (state.get("plan") or {}).get("tasks", [])
        if tasks:
            return "plan", "\n".join(
                f"{index}. {_safe_task_goal(task.get('task_goal'), max_chars=None)}"
                for index, task in enumerate(tasks, 1)
            )
    kind = "activity" if node_name in {"tool_agent", "skill_agent", "analysis_agent"} else "status"
    return kind, _progress_message(node_name, state, status=status)


def _progress_message(
    node_name: str,
    state: dict[str, Any],
    *,
    status: str,
) -> str:
    if node_name == "intent_router":
        if status == "failed":
            return "研究目标识别失败。"
        return "正在理解问题与研究范围。" if status == "started" else "已明确研究目标。"
    if node_name == "planner_agent":
        if status == "failed":
            return "研究计划生成失败。"
        if status == "started":
            return "正在制定研究计划。"
        tasks = (state.get("plan") or {}).get("tasks", [])
        return f"已形成 {len(tasks)} 个研究步骤。"
    active_task = state.get("active_task") or {}
    task_goal = _safe_task_goal(active_task.get("task_goal"))
    if status == "superseded":
        return f"已调整研究流程，按新研究计划继续：{task_goal}"
    if node_name == "tool_agent":
        queue = (state.get("source_attempt_queues") or {}).get(active_task.get("id"))
        if queue and queue.get("message"):
            return str(queue["message"])
        if status == "completed":
            task_result = _matching_task_result(state)
            if task_result and task_result.get("status") != "success":
                detail = recovery_failure_message(error_code=task_result.get("error_code"),
                                                  content=task_result.get("content", ""))
                if detail:
                    return detail
            tool_result = (_matching_tool_result(state)
                           if task_result is None or task_result.get("status") != "success" else None)
            if tool_result and (tool_result.get("error") or {}).get("code") == "provider_permission_denied":
                waiting = any(item.get("task_id") == active_task.get("id")
                              and call_id not in state.get("recovery_outcomes", {})
                              for call_id, item in (state.get("recovery_items") or {}).items())
                return ("数据源权限不足，等待选择来源。" if waiting else
                        "数据源账号权限不足，原接口数据未获取。")
        tool_name = str(active_task.get("tool_name") or "").lower()
        if tool_name.rsplit(".", 1)[-1] == "web_search":
            action = _activity_action("联网检索", state, status=status)
        elif tool_name.startswith("mcp."):
            action = _activity_action("查询市场数据", state, status=status)
        else:
            action = _activity_action("获取研究证据", state, status=status)
        return f"{action}：{task_goal}"
    if node_name == "skill_agent":
        if status == "started":
            return f"正在执行研究流程：{task_goal}"
        if _public_status(node_name, state, status=status) == "pending":
            return f"研究流程需要调整，正在评估后续方案：{task_goal}"
        if _public_status(node_name, state, status=status) == "failed":
            return f"研究流程未完成：{task_goal}"
        return f"研究流程已完成：{task_goal}"
    if node_name == "analysis_agent":
        if status == "started":
            return f"正在分析：{task_goal}"
        if _public_status(node_name, state, status=status) == "failed":
            return f"分析未完成：{task_goal}"
        return f"分析任务已完成：{task_goal}"
    if status == "started":
        return "正在依据已验证证据生成最终回答。"
    if status == "failed":
        return "最终回答生成失败。"
    return "最终回答已生成。"


def _safe_task_goal(value: object, *, max_chars: int | None = 200) -> str:
    text = public_text(str(value or "当前子任务"))
    text = re.sub(
        r"(?i)\b(?:web_search|search_tool|required_schema|tool_name|skill_name|mcp\.[a-z0-9_.-]+)\b",
        "",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip(" ：:，,")
    return (text if max_chars is None else text[:max_chars]) or "当前子任务"


def _matching_task_result(state: dict[str, Any]) -> dict[str, Any] | None:
    task_id = (state.get("active_task") or {}).get("id")
    for result in reversed(state.get("task_results") or []):
        if result.get("task_id") == task_id:
            return result
    return None


def _public_status(node_name: str, state: dict[str, Any], *, status: str) -> str:
    if node_name == "tool_agent" and status == "completed":
        queue = (state.get("source_attempt_queues") or {}).get((state.get("active_task") or {}).get("id"))
        if queue and queue.get("status") in {"ready", "waiting"}:
            return "started"
    if status != "completed" or node_name not in {"tool_agent", "skill_agent", "analysis_agent"}:
        return status
    result = _matching_task_result(state)
    if result is None and node_name == "tool_agent":
        result = _matching_tool_result(state)
    if result and result.get("status") != "success":
        return "pending" if node_name in {"skill_agent", "tool_agent"} else "failed"
    return status


def _matching_tool_result(state: dict[str, Any]) -> dict[str, Any] | None:
    """待来源确认时没有 TaskResult，只读取同任务真实调用的结果。"""
    task_id = (state.get("active_task") or {}).get("id")
    if not task_id:
        return None
    for record in reversed(state.get("tool_calls") or []):
        if not isinstance(record, dict):
            record = record.model_dump(mode="json")
        call_id = (record.get("request") or {}).get("call_id")
        context = (state.get("tool_selection_contexts") or {}).get(call_id, {})
        item = (state.get("recovery_items") or {}).get(call_id, {})
        if (context.get("task") or {}).get("id") == task_id or item.get("task_id") == task_id:
            return record.get("result")
    return None


def _activity_action(action: str, state: dict[str, Any], *, status: str) -> str:
    if status == "started":
        return f"正在{action}"
    if _public_status("tool_agent", state, status=status) == "pending":
        return f"{action}需要调整，正在评估后续方案"
    if status == "failed" or _public_status("tool_agent", state, status=status) == "failed":
        return f"{action}未完成"
    if action == "查询市场数据":
        return "已取得市场数据"
    if action == "获取研究证据":
        return "已取得研究证据"
    return f"已完成{action}"


def _tool_activity_metadata(
    node_name: str,
    state: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    if node_name != "tool_agent":
        return {}
    active_task = state.get("active_task") or {}
    result = _matching_task_result(state)
    tool_name = str((result or {}).get("tool_name") or active_task.get("tool_name") or "").lower()
    queue = (state.get("source_attempt_queues") or {}).get(active_task.get("id"))
    if queue and queue.get("calls") and not result:
        index = min(queue.get("index", 0), len(queue["calls"]) - 1)
        tool_name = queue["calls"][index]["request"]["tool_name"].lower()
    if tool_name.rsplit(".", 1)[-1] == "web_search":
        activity_type = "web_search"
    elif tool_name.startswith("mcp."):
        activity_type = "market_data"
    else:
        activity_type = "evidence"
    task_label = _safe_task_goal(active_task.get("task_goal"))
    metadata: dict[str, Any] = {
        "activity_type": activity_type,
        "group_id": f"{activity_type}:{state.get('planner_iteration', 0)}",
        "task_id": str(active_task.get("id") or ""),
        "task_label": task_label,
    }
    if activity_type != "web_search":
        return metadata

    query = task_label
    urls: list[str] = []
    if status == "completed" and result and result.get("status") == "success":
        try:
            payload = json.loads(str(result.get("content") or ""))
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("search_result"), dict):
            payload = payload["search_result"]
        if isinstance(payload, dict):
            answer = payload.get("answer")
            if isinstance(answer, str) and answer.strip():
                metadata["answer"] = sanitize_untrusted_details(
                    {"answer": public_text(answer).strip()[:4000]}, max_string_length=4000,
                )["answer"][:4000]
            raw_query = payload.get("query")
            if isinstance(raw_query, str) and raw_query.strip():
                query = public_text(raw_query).strip()[:500]
            raw_results = payload.get("results")
            if isinstance(raw_results, list):
                for item in raw_results:
                    if not isinstance(item, dict):
                        continue
                    normalized = _normalize_public_url(item.get("url"))
                    if normalized and normalized not in urls:
                        urls.append(normalized)
    metadata.update({
        "query": query,
        "result_count": len(urls),
        "result_urls": urls,
    })
    return metadata


def _normalize_public_url(value: object) -> str | None:
    if not public_web_url(value):
        return None
    normalized, _fragment = urldefrag(value.strip())
    if urlsplit(normalized).scheme.lower() not in {"http", "https"}:
        return None
    return normalized
