from collections.abc import Callable
import json
import re
from typing import Any
from urllib.parse import urldefrag, urlsplit

from langgraph.config import get_stream_writer

from app.llm.schemas import LlmStreamDelta


_STAGE_LABELS = {
    "intent_router": "理解问题",
    "planner_agent": "制定计划",
    "skill_agent": "执行研究流程",
    "tool_agent": "获取证据",
    "analysis_agent": "分析证据",
    "synthesizer": "生成回答",
}
_SUMMARY_LABELS = {
    "planner_agent": "研究计划",
    "analysis_agent": "研究发现",
    "skill_agent": "研究发现",
}
_INTERNAL_MARKERS = (
    "required_schema",
    "system_prompt",
    "user_prompt",
    "tool_name",
    "skill_name",
    "reasoning_content",
    "research_materials",
    "data_limitations",
)
_INTERNAL_REASONING_PATTERN = re.compile(
    r"(?i)(?:\bthe user\b|\bi must\b|\bi should\b|\blet me\b|"
    r"\bsystem prompt\b|\buser prompt\b|\bjson\b|\bschema\b|"
    r"提示词|内部字段|内部约束|用户(?:要我|希望我)|"
    r"我(?:必须|不能|应该|应当|需要|要先|要给|要注意)|我们需要|不得输出|"
    r"写作|回答结构|结构(?:上)?[:：]|结论先行|核心逻辑链|给出后续|"
    r"长度(?:适中|控制)|markdown)"
)
_PUBLIC_REASONING_SEGMENT_LIMIT = 12
_PUBLIC_REASONING_CHARACTER_LIMIT = 1_600
_PUBLIC_TEXT_LIMIT = 240
_PUBLIC_CONTENT_BATCH_CHARS = 24


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
    kind, message = _public_event_content(node_name, state, status=status)
    public_status = _public_status(node_name, state, status=status)
    event = {
        "type": "progress",
        "kind": kind,
        "id": _progress_id(node_name, state),
        "stage": node_name,
        "label": _SUMMARY_LABELS.get(node_name, label) if kind == "summary" else label,
        "status": public_status,
        "message": message,
    }
    event.update(_web_search_progress_metadata(node_name, state))
    writer(event)


def build_public_llm_stream_handler(
    node_name: str,
    state: dict[str, Any],
    *,
    stream_content: bool = False,
    stream_reasoning: bool = False,
) -> "PublicLlmStreamHandler":
    del state
    try:
        writer = get_stream_writer()
    except RuntimeError:
        writer = None
    return PublicLlmStreamHandler(
        node_name=node_name,
        writer=writer,
        stream_content=stream_content,
        stream_reasoning=stream_reasoning and node_name == "synthesizer",
    )


class PublicLlmStreamHandler:
    def __init__(
        self,
        *,
        node_name: str,
        writer: Callable[[dict[str, Any]], None] | None,
        stream_content: bool,
        stream_reasoning: bool,
    ) -> None:
        self._node_name = node_name
        self._writer = writer
        self._stream_content = stream_content
        self._stream_reasoning = stream_reasoning
        self._reasoning_buffer = ""
        self._content_buffer = ""
        self._reasoning_segments_emitted = 0
        self._reasoning_characters_emitted = 0

    def __call__(self, event: LlmStreamDelta) -> None:
        if self._writer is None:
            return
        if event.kind == "reasoning" and self._stream_reasoning:
            self._flush_content()
            self._reasoning_buffer += event.delta
            self._emit_complete_reasoning_segments()
            return
        if event.kind == "content" and self._stream_content:
            self._flush_reasoning()
            self._content_buffer += event.delta
            if (
                len(self._content_buffer) >= _PUBLIC_CONTENT_BATCH_CHARS
                or any(mark in event.delta for mark in "\n。！？")
            ):
                self._flush_content()

    def flush(self) -> None:
        self._flush_reasoning()
        self._flush_content()

    def _flush_reasoning(self) -> None:
        if not self._reasoning_buffer:
            return
        segment = self._reasoning_buffer
        self._reasoning_buffer = ""
        self._emit_reasoning_segment(segment)

    def _flush_content(self) -> None:
        if not self._content_buffer or self._writer is None:
            return
        content = self._content_buffer
        self._content_buffer = ""
        self._writer({"type": "answer_delta", "content": content})

    def _emit_complete_reasoning_segments(self) -> None:
        while True:
            boundary = re.search(r"[。！？\n]", self._reasoning_buffer)
            if boundary is None:
                return
            end = boundary.end()
            segment = self._reasoning_buffer[:end]
            self._reasoning_buffer = self._reasoning_buffer[end:]
            self._emit_reasoning_segment(segment)

    def _emit_reasoning_segment(self, segment: str) -> None:
        if self._writer is None:
            return
        content = _safe_public_reasoning_text(segment)
        if not content:
            return
        if self._reasoning_segments_emitted >= _PUBLIC_REASONING_SEGMENT_LIMIT:
            return
        remaining = _PUBLIC_REASONING_CHARACTER_LIMIT - self._reasoning_characters_emitted
        if remaining <= 0:
            return
        content = content[:remaining].rstrip()
        if not content:
            return
        self._reasoning_segments_emitted += 1
        self._reasoning_characters_emitted += len(content)
        self._writer(
            {
                "type": "thinking_delta",
                "id": f"{self._node_name}:reasoning",
                "stage": self._node_name,
                "label": "分析证据",
                "content": f"- {content}\n",
            }
        )


def _safe_public_reasoning_text(value: object) -> str | None:
    content = _safe_public_text(value)
    if not content or _INTERNAL_REASONING_PATTERN.search(content):
        return None
    visible_characters = [character for character in content if character.isalnum()]
    chinese_characters = re.findall(r"[\u3400-\u9fff]", content)
    if visible_characters and len(chinese_characters) / len(visible_characters) < 0.2:
        return None
    return content


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
    if status == "completed" and node_name == "planner_agent":
        summary = _safe_public_text((state.get("plan") or {}).get("public_summary"))
        if summary:
            return "summary", summary
    if status == "completed" and node_name in {"analysis_agent", "skill_agent"}:
        summary = _task_result_summary(state)
        if summary:
            return "summary", summary
    kind = "activity" if node_name in {"tool_agent", "skill_agent"} else "status"
    return kind, _progress_message(node_name, state, status=status)


def _progress_message(
    node_name: str,
    state: dict[str, Any],
    *,
    status: str,
) -> str:
    if node_name == "intent_router":
        return "正在理解问题与研究范围。" if status == "started" else "已明确研究目标。"
    if node_name == "planner_agent":
        if status == "started":
            return "正在制定研究计划。"
        tasks = (state.get("plan") or {}).get("tasks", [])
        return f"已形成 {len(tasks)} 个研究步骤。"
    active_task = state.get("active_task") or {}
    task_goal = _safe_task_goal(active_task.get("task_goal"))
    if node_name == "tool_agent":
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
        if _public_status(node_name, state, status=status) == "failed":
            return f"研究流程未完成：{task_goal}"
        return f"研究流程已完成：{task_goal}"
    if node_name == "analysis_agent":
        if status == "started":
            return f"正在分析：{task_goal}"
        if status == "failed":
            return f"分析未完成：{task_goal}"
        return f"分析任务已完成：{task_goal}"
    if status == "started":
        return "正在依据已验证证据生成最终回答。"
    if status == "failed":
        return "最终回答生成失败。"
    return "最终回答已生成。"


def _task_result_summary(state: dict[str, Any]) -> str | None:
    result = _matching_task_result(state)
    if result and result.get("status") == "success":
        return _safe_public_text(result.get("content"))
    return None


def _safe_public_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith(("{", "[")):
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in _INTERNAL_MARKERS):
        return None
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^[-*+]\s+", "", text)
    text = re.sub(r"\s+", " ", text)
    if len(text) > _PUBLIC_TEXT_LIMIT:
        text = text[: _PUBLIC_TEXT_LIMIT - 1].rstrip("，,；;。 ") + "…"
    return text


def _safe_task_goal(value: object) -> str:
    text = str(value or "当前子任务")
    text = re.sub(
        r"(?i)\b(?:web_search|search_tool|required_schema|tool_name|skill_name|mcp\.[a-z0-9_.-]+)\b",
        "",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip(" ：:，,")
    return text[:200] or "当前子任务"


def _matching_task_result(state: dict[str, Any]) -> dict[str, Any] | None:
    task_id = (state.get("active_task") or {}).get("id")
    for result in reversed(state.get("task_results") or []):
        if result.get("task_id") == task_id:
            return result
    return None


def _public_status(node_name: str, state: dict[str, Any], *, status: str) -> str:
    if status != "completed" or node_name not in {"tool_agent", "skill_agent"}:
        return status
    result = _matching_task_result(state)
    return "failed" if result and result.get("status") != "success" else status


def _activity_action(action: str, state: dict[str, Any], *, status: str) -> str:
    if status == "started":
        return f"正在{action}"
    if status == "failed" or _public_status("tool_agent", state, status=status) == "failed":
        return f"{action}未完成"
    if action == "查询市场数据":
        return "已取得市场数据"
    if action == "获取研究证据":
        return "已取得研究证据"
    return f"已完成{action}"


def _web_search_progress_metadata(
    node_name: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    if node_name != "tool_agent":
        return {}
    active_task = state.get("active_task") or {}
    tool_name = str(active_task.get("tool_name") or "").lower()
    if tool_name.rsplit(".", 1)[-1] != "web_search":
        return {}
    query = _safe_task_goal(active_task.get("task_goal"))
    urls: list[str] = []
    result = _matching_task_result(state)
    if result and result.get("status") == "success":
        try:
            payload = json.loads(str(result.get("content") or ""))
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            raw_query = payload.get("query")
            if isinstance(raw_query, str) and raw_query.strip():
                query = raw_query.strip()[:500]
            raw_results = payload.get("results")
            if isinstance(raw_results, list):
                for item in raw_results:
                    if not isinstance(item, dict):
                        continue
                    normalized = _normalize_public_url(item.get("url"))
                    if normalized and normalized not in urls:
                        urls.append(normalized)
    return {
        "activity_type": "web_search",
        "group_id": f"web_search:{state.get('planner_iteration', 0)}",
        "task_id": str(active_task.get("id") or ""),
        "query": query,
        "result_count": len(urls),
        "result_urls": urls,
    }


def _normalize_public_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized, _fragment = urldefrag(value.strip())
    if urlsplit(normalized).scheme.lower() not in {"http", "https"}:
        return None
    return normalized
