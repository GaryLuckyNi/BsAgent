"""决策头校验后的正文按句发布，不公开补证申请或旧协议 JSON。"""

import re
from collections.abc import Callable

from app.graph.planning.output_checks import has_prohibited_advice
from app.graph.planning.synthesis_decision import is_answer_header
from app.llm.schemas import LlmStreamDelta

_UNIT_END = re.compile(r"[。！？；]|\r?\n")
_PROTECTED = re.compile(
    r"(?P<fence>`{3,}|~{3,})[\s\S]*?(?:(?P=fence)|\Z)"
    r"|(?P<ticks>`+)[^\n]*?(?:(?P=ticks)|\Z)"
    r"|!?\[[^\]\n]*(?:\]\([^\n]*?(?:\)|\Z)|\]|\Z)"
    r"|https?://[^\s<>]+"
)


def _unit_boundary(content: str) -> re.Match | None:
    """句末或行末即可发布；未闭合的代码、链接留待后续片段。"""
    protected = iter(_PROTECTED.finditer(content))
    span = next(protected, None)
    for boundary in _UNIT_END.finditer(content):
        while span is not None and span.end() <= boundary.start():
            span = next(protected, None)
        if span is None or boundary.start() < span.start():
            return boundary
    return None


class SynthesisStream:
    def __init__(
        self,
        *,
        on_delta: Callable[[LlmStreamDelta], None],
        transform: Callable[[str], str] = lambda text: text,
        validate: Callable[[str], bool] = lambda text: True,
        require_decision_header: bool = True,
    ):
        self._on_delta = on_delta
        self._transform = transform
        self._validate = validate
        self._published = ""
        self._buffer = ""
        self._header_checked = not require_decision_header
        self._blocked = False
        self.answer_started = False
        self._body_started = False

    def __call__(self, event: LlmStreamDelta) -> None:
        if event.kind != "content" or self._blocked:
            return
        self._buffer += event.delta
        if not self._header_checked:
            header, newline, rest = self._buffer.partition("\n")
            if len(header) > 4096:
                self._blocked = True
            elif not newline:
                return
            else:
                self._header_checked = True
                self._blocked = not is_answer_header(header)
                self.answer_started = not self._blocked
                self._buffer = rest
            if self._blocked:
                self._buffer = ""
                return
        if not self._body_started:
            self._buffer = self._buffer.lstrip()
            self._body_started = bool(self._buffer)
        while not self._blocked and (boundary := _unit_boundary(self._buffer)):
            paragraph, self._buffer = self._buffer[:boundary.end()], self._buffer[boundary.end():]
            self._publish(paragraph)

    def finish(self) -> None:
        """仅完整响应解析成功后调用；中断时不发布未闭合尾句。"""
        if self._header_checked and not self._blocked and self._buffer:
            self._publish(self._buffer.rstrip())
        self._buffer = ""

    def _publish(self, content: str) -> None:
        candidate = self._published + content
        if has_prohibited_advice(candidate) or not self._validate(candidate):
            self._blocked = True
            self._buffer = ""
            return
        self._published = candidate
        content = self._transform(content)
        if content:
            self._on_delta(LlmStreamDelta(kind="content", delta=content))
