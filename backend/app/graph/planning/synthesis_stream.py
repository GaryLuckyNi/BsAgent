"""决策头校验后增量发布正文，保留未闭合保护单元和短尾。"""

import re
from collections.abc import Callable

from app.graph.planning.output_checks import has_prohibited_advice
from app.graph.planning.synthesis_decision import is_answer_header
from app.llm.schemas import LlmStreamDelta

_UNIT_END = re.compile(r"[。！？；]|\r?\n")
_INCREMENT_END = re.compile(r"[\s\u3000-\u303f\u4e00-\u9fff]")
_NUMBER_UNIT = re.compile(r"\d[\d,.]*\s*(?:亿元|万元|千元|元|亿|万|%|％|年|月|日|倍|个百分点)?")
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


def _increment_boundary(content: str) -> int:
    """无句末时也可发布稳定前文；数字、代码、URL和引用不能切成半个单元。"""
    limit = len(content) - 24
    if limit < 24:
        return 0
    spans = tuple(_PROTECTED.finditer(content)) + tuple(_NUMBER_UNIT.finditer(content))
    limit = min((span.start() for span in spans if span.start() < limit < span.end()), default=limit)
    cut = 0
    for match in _INCREMENT_END.finditer(content, 0, max(0, limit)):
        cut = match.end()
    for protected in sorted(spans, key=lambda span: span.start(), reverse=True):
        if protected.start() < cut < protected.end():
            cut = protected.start()
    return cut


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
            self._buffer = self._buffer.lstrip()
            header, newline, rest = self._buffer.partition("\n")
            if len(header) > 4096:
                self._blocked = True
            elif not newline:
                return
            else:
                self._header_checked = True
                self._blocked = not is_answer_header(header)
                self._buffer = rest
            if self._blocked:
                self._buffer = ""
                return
        if not self._body_started:
            self._buffer = self._buffer.lstrip()
            self._body_started = bool(self._buffer)
        while not self._blocked:
            boundary = _unit_boundary(self._buffer)
            cut = boundary.end() if boundary else _increment_boundary(self._buffer)
            if not cut:
                break
            paragraph, self._buffer = self._buffer[:cut], self._buffer[cut:]
            self._publish(paragraph, lookahead=self._buffer)

    def finish(self) -> None:
        """仅完整响应解析成功后调用；中断时不发布未闭合尾句。"""
        if self._header_checked and not self._blocked and self._buffer:
            self._publish(self._buffer.rstrip())
        self._buffer = ""

    def _publish(self, content: str, *, lookahead: str = "") -> None:
        candidate = self._published + content
        if has_prohibited_advice(candidate + lookahead) or not self._validate(candidate):
            self._blocked = True
            self._buffer = ""
            return
        self._published = candidate
        content = self._transform(content)
        if content:
            self._on_delta(LlmStreamDelta(kind="content", delta=content))
            self.answer_started = True
