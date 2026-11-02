"""从正在生成的结构中投影公开字段；草稿不参与规划或执行。"""

from collections.abc import Callable
from time import monotonic
from typing import Literal

from langchain_core.utils.json import parse_partial_json

from app.llm.schemas import LlmStreamDelta


class ProgressFieldStream:
    def __init__(
        self, *, kind: Literal["goal", "plan"], on_text: Callable[[str], None],
        feedback: bool = False, clean: Callable[[str], str] = str.strip,
        clock: Callable[[], float] = monotonic,
        on_failed: Callable[[], None] | None = None,
    ):
        self._kind = kind
        self._on_text = on_text
        self._feedback = feedback
        self._clean = clean
        self._clock = clock
        self._on_failed = on_failed
        self._buffer = ""
        self._latest = ""
        self._shown = ""
        self._last_publish = 0.0

    def __call__(self, event: LlmStreamDelta) -> None:
        if event.kind != "content":
            return
        self._buffer += event.delta
        # 只接受 JSON 对象，不公开其余文本或提供方 reasoning。
        if not self._buffer.lstrip().startswith("{") or len(self._buffer) > 131_072:
            return
        try:
            value = parse_partial_json(self._buffer)
        except (ValueError, TypeError):
            return
        if not isinstance(value, dict):
            return
        if self._kind == "goal":
            goal = value.get("goal")
            self._latest = self._clean(goal) if isinstance(goal, str) else ""
        else:
            if self._feedback:
                value = value.get("plan") if value.get("action") == "revise" else None
            tasks = value.get("tasks") if isinstance(value, dict) else None
            self._latest = "\n".join(
                f"{index}. {self._clean(task['task_goal'])}"
                for index, task in enumerate(tasks[:12] if isinstance(tasks, list) else [], 1)
                if isinstance(task, dict) and isinstance(task.get("task_goal"), str)
                and task["task_goal"].strip()
            )
        if not self._latest or self._latest == self._shown:
            return
        if (not self._shown or len(self._latest) - len(self._shown) >= 24
                or self._clock() - self._last_publish >= 0.08):
            self.flush()

    def flush(self) -> None:
        if self._latest and self._latest != self._shown:
            self._shown = self._latest
            self._last_publish = self._clock()
            self._on_text(self._latest)

    def fail(self) -> None:
        if self._shown and self._on_failed is not None:
            self._on_failed()
        self.restart()

    def restart(self) -> None:
        """新一轮模型响应使用独立 JSON 缓冲；能力补检索不发布失败状态。"""
        self._latest = self._shown = self._buffer = ""
        self._last_publish = 0.0
