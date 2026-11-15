"""把 SDK 自动采集的 JSON 转为经过投影和内容策略处理的观测属性。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from langfuse.types import OtelSpanData

from app.operations.langfuse_content import LangfuseContentPolicy
from app.operations.langfuse_scheduler import scheduler_trace_input, scheduler_trace_update

_OBSERVATION_PREFIX = "langfuse.observation."
_APPLIED_ATTRIBUTE = _OBSERVATION_PREFIX + "metadata.content_policy_applied"
# 独立于输出上限，避免先截断原文而破坏 JSON 密钥识别。
_MAX_INPUT_CHARS = 1_000_000
_MAX_DECODE_CHARS = 2_000_000
_MAX_DEPTH = 32
_MAX_JSON_FRAGMENTS = 1024
_JSON_TOKEN_START = re.compile(
    r'\{\s*(?:"|})|\[\s*(?:["{\[\]]|'
    r'(?:-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)(?=\s*[,\]]))|"'
)
_ENCODED_CONTAINER_PREFIX = re.compile(r'["\\]*\s*[\[{]\s*\\*"')
_NODE_FIELDS = {
    "task_scheduler": {"input": frozenset(), "output": frozenset()},
    "intent_router": {
        "input": frozenset({"user_message", "messages", "image_analyses"}),
        "output": frozenset({"intent", "errors"}),
    },
    "planner_agent": {
        "input": frozenset(
            {
                "user_message",
                "intent",
                "messages",
                "plan",
                "task_results",
                "planner_iteration",
            }
        ),
        "output": frozenset(
            {
                "plan",
                "planner_decision",
                "planner_stop_reason",
                "planner_iteration",
                "errors",
            }
        ),
    },
}
_IMAGE_SUMMARY_FIELDS = frozenset(
    {
        "analysis_id",
        "document_type",
        "summary",
        "entities",
        "risk_flags",
        "warnings",
        "confidence",
    }
)


@dataclass
class _DecodeBudget:
    remaining: int = _MAX_DECODE_CHARS
    fragments_remaining: int = _MAX_JSON_FRAGMENTS

    def spend(self, chars: int) -> None:
        self.remaining -= chars
        if self.remaining < 0:
            raise ValueError("LangChain JSON decode budget exceeded")

    def load(self, value: str) -> Any:
        self.spend(len(value))
        return json.loads(value, parse_constant=_reject_nonfinite)

    def fragment(self, value: str, start: int) -> tuple[Any, int]:
        self.fragments_remaining -= 1
        if self.fragments_remaining < 0:
            raise ValueError("LangChain JSON fragment budget exceeded")
        try:
            decoded, end = json.JSONDecoder(parse_constant=_reject_nonfinite).raw_decode(
                value, start
            )
        except json.JSONDecodeError:
            # 无闭合引号时解析可能扫描整个后缀，失败也必须消耗工作预算。
            self.spend(len(value) - start)
            raise
        self.spend(end - start)
        return decoded, end


def _reject_nonfinite(value: str) -> Any:
    raise ValueError("Non-finite JSON number")


def _decode_nested(
    value: Any, *, budget: _DecodeBudget, policy: LangfuseContentPolicy, depth: int = 0
) -> Any:
    """先展开嵌套 JSON 字符串，确保工具参数中的敏感键也进入统一策略。"""
    if depth > _MAX_DEPTH:
        raise ValueError("LangChain content depth exceeded")
    if isinstance(value, dict):
        return {
            key: _decode_nested(item, budget=budget, policy=policy, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _decode_nested(item, budget=budget, policy=policy, depth=depth + 1) for item in value
        ]
    if isinstance(value, str):
        if value.lstrip().startswith('"'):
            try:
                decoded = budget.load(value)
            except json.JSONDecodeError:
                # 以引号开头的自然语言仍需继续检查内联 JSON。
                pass
            else:
                return _decode_nested(decoded, budget=budget, policy=policy, depth=depth + 1)
        return _sanitize_inline_json(value, budget=budget, policy=policy, depth=depth)
    return value


def _sanitize_inline_json(
    value: str, *, budget: _DecodeBudget, policy: LangfuseContentPolicy, depth: int
) -> Any:
    """识别正文中的 JSON 容器与字符串，普通括号和引号保留原样。"""
    pieces: list[str] = []
    cursor = 0
    for match in _JSON_TOKEN_START.finditer(value):
        start = match.start()
        if start < cursor:
            continue
        # 明显是 JSON 却不完整时，让上层丢弃字段；不得退回未经清理的正文。
        try:
            decoded, end = budget.fragment(value, start)
        except json.JSONDecodeError:
            if value[start] != '"' or _ENCODED_CONTAINER_PREFIX.match(value, start):
                raise
            continue
        expanded = _decode_nested(decoded, budget=budget, policy=policy, depth=depth + 1)
        if cursor == 0 and not value[:start].strip() and not value[end:].strip():
            return expanded
        pieces.extend((value[cursor:start], _dump(policy.sanitize(expanded))))
        cursor = end
    if not pieces:
        return value
    pieces.append(value[cursor:])
    return "".join(pieces)


def _project_state(value: Any, *, node: str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("LangGraph node content must be a state object")
    if node == "task_scheduler":
        return scheduler_trace_input(value) if field == "input" else scheduler_trace_update(value)
    projected = {key: item for key, item in value.items() if key in _NODE_FIELDS[node][field]}
    if "image_analyses" in projected:
        images = projected["image_analyses"]
        if not isinstance(images, list):
            raise ValueError("Image summaries must be a list")
        projected["image_analyses"] = [
            {key: item for key, item in image.items() if key in _IMAGE_SUMMARY_FIELDS}
            for image in images
            if isinstance(image, dict)
        ]
    return projected


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _bounded_json(value: Any, *, limit: int) -> str:
    serialized = _dump(value)
    if len(serialized) <= limit:
        return serialized

    def preview(retained: int) -> str:
        return _dump(
            {
                "truncated": True,
                "content": serialized[:retained],
                "original_chars": len(serialized),
                "retained_chars": retained,
            }
        )

    if len(preview(0)) > limit:
        return '""' if limit >= 2 else "0"
    low, high = 0, min(limit, len(serialized))
    while low < high:
        middle = (low + high + 1) // 2
        if len(preview(middle)) <= limit:
            low = middle
        else:
            high = middle - 1
    return preview(low)


def sanitized_content_json(value: Any, *, policy: LangfuseContentPolicy) -> str:
    """保留已限长的 JSON 字符串，避免 SDK 再序列化中文而突破上限。"""
    expanded = _decode_nested(value, budget=_DecodeBudget(), policy=policy)
    return _bounded_json(policy.sanitize(expanded), limit=policy.max_chars)


def sanitized_langchain_io(
    span: OtelSpanData, *, policy: LangfuseContentPolicy
) -> dict[str, str | bool]:
    """仅放开自动 generation 和选定业务节点的 observation 级安全内容。"""
    attributes = span.attributes
    if (
        policy.mode == "metadata_only"
        or span.instrumentation_scope_name != "langfuse-sdk"
        or attributes.get(_APPLIED_ATTRIBUTE) is True
    ):
        return {}
    kind = attributes.get(_OBSERVATION_PREFIX + "type")
    is_generation = kind == "generation"
    if not is_generation and not (span.name in _NODE_FIELDS and kind in {"chain", "span", "agent"}):
        return {}

    result: dict[str, str | bool] = {}
    for field in ("input", "output"):
        key = _OBSERVATION_PREFIX + field
        raw = attributes.get(key)
        if not isinstance(raw, str) or len(raw) > _MAX_INPUT_CHARS:
            continue
        try:
            budget = _DecodeBudget()
            value = budget.load(raw)
            if not is_generation:
                value = _project_state(value, node=span.name, field=field)
                if not value and not (span.name == "task_scheduler" and field == "output"):
                    continue
            expanded = _decode_nested(value, budget=budget, policy=policy)
            result[key] = _bounded_json(policy.sanitize(expanded), limit=policy.max_chars)
        except Exception:  # noqa: BLE001, S112
            # 观测失败不能影响请求，也不记录可能包含原文的异常；取消仍向上传播。
            continue
    if result:
        result[_APPLIED_ATTRIBUTE] = True
    return result
