"""模型完成后的安全副本和有界流缓冲；尚未检查的正文不向调用方发布。"""

import re
from contextlib import asynccontextmanager, contextmanager
from functools import lru_cache

from app.llm.schemas import LlmStreamDelta
from app.security.tool_content import ToolContentPolicy

WITHHELD_OUTPUT = "[模型输出内容已扣留]"


@contextmanager
def closing_model_source(source):
    from app.security.http_response import close_http_response
    interrupted = True
    try:
        yield source
        interrupted = False
    finally:
        if callable(getattr(source, "close", None)):
            close_http_response(source, interrupted=interrupted)


@asynccontextmanager
async def closing_async_model_source(source):
    from app.security.http_response import aclose_http_response
    interrupted = True
    try:
        yield source
        interrupted = False
    finally:
        if callable(getattr(source, "aclose", None)):
            await aclose_http_response(source, interrupted=interrupted)


class ModelOutputRejected(RuntimeError):
    code = "model_output_rejected"

    def __init__(self):
        super().__init__("模型输出内容受限。")


class ModelOutputUnavailable(RuntimeError):
    code = "model_output_unavailable"

    def __init__(self):
        super().__init__("模型输出检查暂不可用。")


@lru_cache(maxsize=1)
def _output_policy():
    return ToolContentPolicy(
        max_string_chars=1_000_000, max_total_chars=2_000_000, max_nodes=50_000,
        scan_injection=False,
    )


def output_view(value, *, allow_withheld_text=True):
    failed = False
    try:
        view = _output_policy().project(value, tool_name="model-output", sink="workflow")
    except Exception:  # noqa: BLE001 - 在 except 外抛固定错误，避免原异常链外传。
        failed = True
    if failed:
        raise ModelOutputUnavailable
    if view.metadata.disposition == "withheld":
        if isinstance(value, str) and allow_withheld_text:
            return WITHHELD_OUTPUT
        raise ModelOutputRejected
    return view.data


class IncrementalTextGuard:
    """普通正文留24字符尾部；可能开启凭证上下文的后缀完整检查后释放。"""

    _risk = re.compile(
        r"(?<![a-z])(?:api|access|refresh|client|connection|database|private|"
        r"authorization|credential|password|secret|token|bearer|cookie|dsn|sk-|AKIA|ASIA)"
        r"|://|-----|\{|\\(?:u|[\"'\\])|%[0-9a-f]{2}", re.IGNORECASE
    )
    _boundary = re.compile(r"[\s\u3000-\u303f\u4e00-\u9fff]")
    _answer_envelope = re.compile(r'^\s*\{\s*"action"\s*:\s*"answer"\s*\}\r?\n')
    _url_end = re.compile(r'''[\s<>"']''')

    def __init__(self):
        self.pending = ""
        self.held = None
        self.chars = 0
        self.closed = False
        self._prefix_active = True
        self._prefix_buffer = ""
        self._url_start = None
        self._held_size = 0

    def feed(self, text):
        if self.closed or not isinstance(text, str):
            raise ModelOutputRejected
        self.chars += len(text)
        if self.chars > 1_000_000:
            raise ModelOutputRejected
        if not text:
            return ""
        prefix = ""
        if self._prefix_active:
            self._prefix_buffer += text
            text = self._prefix_buffer
            match = self._answer_envelope.match(text)
            if match:
                # 仅释放无附加字段的完整控制头；不将其作为身份或授权。
                prefix, text = output_view(text[:match.end()]), text[match.end():]
            elif len(text) <= 128 and (
                not text.strip() or (text.lstrip().startswith("{") and "\n" not in text.lstrip())
            ):
                return ""
            self._prefix_active, self._prefix_buffer = False, ""
        return prefix + self._feed(text)

    def _release_public_url(self, probe):
        if self._url_start is None:
            return None
        base = self._held_size - len(probe)
        boundary = self._url_end.search(probe, max(0, self._url_start - base))
        if boundary is None:
            return None
        content = "".join(self.held)
        cut = base + boundary.end()
        unit = content[:cut]
        if output_view(unit) != unit:
            # 有凭证或畸形材料的URL继续保守缓冲，不能只清理半个值。
            self._url_start = None
            return None
        self.pending = content[cut:]
        self.held, self._held_size, self._url_start = None, 0, None
        return unit

    def _feed(self, text):
        output = []
        if self.held is not None:
            self.held.append(text)
            self._held_size += len(text)
            released = self._release_public_url(text)
            if released is None:
                return ""
            output.append(released)
        else:
            self.pending += text
        # 限制单片中大量保护单元的处理次数，剩余内容完整检查后再释放。
        for _ in range(128):
            risk = self._risk.search(self.pending)
            limit = max(0, (risk.start() if risk else len(self.pending)) - 24)
            cut = 0
            for match in self._boundary.finditer(self.pending, 0, limit):
                cut = match.end()
            prefix, self.pending = self.pending[:cut], self.pending[cut:]
            if prefix:
                output.append(output_view(prefix))
            if risk:
                self._url_start = risk.end() - cut if risk[0] == "://" else None
                self.held, self._held_size = [self.pending], len(self.pending)
                self.pending = ""
                released = self._release_public_url(self.held[-1])
                if released is not None:
                    output.append(released)
                    continue
            elif not cut and len(self.pending) > 256:
                self.held, self._held_size = [self.pending], len(self.pending)
                self.pending = ""
            break
        else:
            self.held, self._held_size, self._url_start = [self.pending], len(self.pending), None
            self.pending = ""
        return "".join(output)

    def finish(self):
        if self.closed:
            return ""
        self.closed = True
        tail = self._prefix_buffer + ("".join(self.held) if self.held is not None else self.pending)
        self.held, self.pending = None, ""
        return output_view(tail) if tail else ""


class IncrementalOutput:
    def __init__(self):
        self.guards = {kind: IncrementalTextGuard() for kind in ("content", "reasoning")}
        self.control = OutputBuffer()
        self.count = self.chars = 0
        self.order = []

    def add(self, event):
        if isinstance(event, str):
            event = LlmStreamDelta(kind="content", delta=event)
        if not isinstance(event, LlmStreamDelta):
            raise ModelOutputRejected
        self.count += 1
        self.chars += len(event.delta)
        if self.count > 32768 or self.chars > 2_000_000:
            raise ModelOutputRejected
        if event.kind in self.guards:
            if event.kind not in self.order:
                self.order.append(event.kind)
            text = self.guards[event.kind].feed(event.delta)
            return [LlmStreamDelta(kind=event.kind, delta=text)] if text else []
        self.control.add(event)
        return []

    def finish(self):
        if not self.count:
            from app.llm.errors import LlmProviderError
            raise LlmProviderError(code="llm_invalid_response")
        result = []
        for kind in self.order:
            guard = self.guards[kind]
            text = guard.finish()
            if text:
                result.append(LlmStreamDelta(kind=kind, delta=text))
        if self.control.seen:
            result.extend(self.control.events())
        return result


def protect_completion(result):
    from app.llm.schemas import LlmCompletionResult

    if not isinstance(result, LlmCompletionResult):
        raise ModelOutputRejected
    failed = False
    try:
        data = result.model_dump(mode="json")
    except Exception:  # noqa: BLE001 - 不保留第三方序列化器异常链。
        failed = True
    if failed:
        raise ModelOutputUnavailable
    data["content"] = output_view(result.content)
    data["reasoning_content"] = output_view(result.reasoning_content)
    return result.model_copy(update=output_view(data))


def emit_completion(result, callback):
    if callback is None or result.finish_reason == "length":
        return
    if result.reasoning_content:
        callback(LlmStreamDelta(kind="reasoning", delta=result.reasoning_content))
    if result.content:
        callback(LlmStreamDelta(kind="content", delta=result.content))


class OutputBuffer:
    def __init__(self):
        self.parts = {"content": [], "reasoning": []}
        self.order = []
        self.usage = {}
        self.finish_reason = None
        self.nodes = 0
        self.chars = 0
        self.seen = False

    def add(self, event):
        if isinstance(event, str):
            event = LlmStreamDelta(kind="content", delta=event)
        if not isinstance(event, LlmStreamDelta):
            raise ModelOutputRejected
        self.nodes += 1
        self.chars += len(event.delta)
        if self.nodes > 32768 or self.chars > 2_000_000:
            raise ModelOutputRejected
        self.seen = True
        if event.kind in self.parts:
            if event.kind not in self.order:
                self.order.append(event.kind)
            self.parts[event.kind].append(event.delta)
        elif event.kind == "finish":
            self.finish_reason = event.delta
        elif event.kind == "usage":
            self.usage.update(event.usage)

    def events(self):
        if not self.seen:
            from app.llm.errors import LlmProviderError

            raise LlmProviderError(code="llm_invalid_response")
        checked = output_view(
            {kind: output_view("".join(parts)) for kind, parts in self.parts.items()}
        )
        events = []
        for kind in self.order:
            if checked[kind]:
                events.append(LlmStreamDelta(kind=kind, delta=checked[kind]))
        if self.finish_reason:
            events.append(LlmStreamDelta(kind="finish", delta=output_view(self.finish_reason)))
        if self.usage:
            usage = output_view(self.usage)
            events.append(
                LlmStreamDelta(
                    kind="usage",
                    usage={
                        key: value
                        for key, value in usage.items()
                        if isinstance(value, (int, float)) and not isinstance(value, bool)
                    },
                )
            )
        return events


def protect_message_response(message):
    from langchain_core.messages import AIMessage, AIMessageChunk

    if not isinstance(message, AIMessage):
        return output_view(message)
    original = message.model_dump(mode="python")
    # 已选择的执行参数不能先改写再交给工具执行层。
    calls = list(message.tool_calls)
    calls.extend(
        call.get("function", {}) | {"id": call.get("id")}
        for call in message.additional_kwargs.get("tool_calls", ())
    )
    legacy = message.additional_kwargs.get("function_call")
    if legacy:
        calls.append(legacy)
    for call in calls:
        args = call.get("args", call.get("arguments"))
        if output_view(args, allow_withheld_text=False) != args:
            raise ModelOutputRejected
        for field in ("id", "name"):
            if output_view(call.get(field)) != call.get(field):
                raise ModelOutputRejected
    data = output_view(original)
    if isinstance(message, AIMessageChunk) and data.get("tool_call_chunks") != original.get(
        "tool_call_chunks"
    ):
        raise ModelOutputRejected
    for field in ("id", "name"):
        if data.get(field) != original.get(field):
            raise ModelOutputRejected
    return message.model_copy(update=data)


class MessageBuffer:
    """合并原生工具调用 chunk，再检查完整参数，避免跨片段绕过。"""

    def __init__(self):
        self.message = None
        self.chars = 0
        self.count = 0

    def add(self, chunk):
        from langchain_core.messages import AIMessageChunk

        self.count += 1
        if not isinstance(chunk, (AIMessageChunk, str)):
            raise ModelOutputRejected
        self.chars += (
            len(chunk.model_dump_json()) if isinstance(chunk, AIMessageChunk) else len(chunk)
        )
        if self.count > 32768 or self.chars > 2_000_000:
            raise ModelOutputRejected
        self.message = chunk if self.message is None else self.message + chunk

    def checked(self):
        if self.message is None:
            from app.llm.errors import LlmProviderError

            raise LlmProviderError(code="llm_invalid_response")
        return protect_message_response(self.message)


class IncrementalMessages:
    """仅提前释放普通文本；工具参数、元数据和完整调用配对仍在结束时验证。"""

    def __init__(self):
        self.buffer = MessageBuffer()
        self.guard = IncrementalTextGuard()
        self.text_mode = True
        self.published = []

    def add(self, chunk):
        from langchain_core.messages import AIMessageChunk

        from app.security.model_input import check_protocol_identifier

        self.buffer.add(chunk)
        content = chunk if isinstance(chunk, str) else chunk.content
        if not isinstance(content, str):
            self.text_mode = False
            return []
        if not self.text_mode:
            return []
        text = self.guard.feed(content)
        if not text:
            return []
        self.published.append(text)
        if isinstance(chunk, str):
            return [text]
        check_protocol_identifier(chunk.id)
        check_protocol_identifier(chunk.name)
        return [AIMessageChunk(content=text, id=chunk.id, name=chunk.name)]

    def finish(self):
        checked = self.buffer.checked()
        if not self.text_mode:
            prefix = "".join(self.published)
            blocks = []
            for block in checked.content:
                text = block if isinstance(block, str) else block.get("text") if block.get("type") == "text" else None
                if prefix and isinstance(text, str):
                    if prefix.startswith(text):
                        prefix = prefix[len(text):]
                        continue
                    if not text.startswith(prefix):
                        raise ModelOutputRejected
                    text, prefix = text[len(prefix):], ""
                    block = text if isinstance(block, str) else {**block, "text": text}
                blocks.append(block)
            if prefix:
                raise ModelOutputRejected
            return [checked.model_copy(update={"content": blocks})]
        tail = self.guard.finish()
        return [tail if isinstance(checked, str) else checked.model_copy(update={"content": tail})]


def _event_view(value, *, depth=0):
    from langchain_core.messages import BaseMessage

    if depth > 32:
        raise ModelOutputRejected
    if isinstance(value, BaseMessage):
        if value.type in {"ai", "AIMessageChunk"}:
            return protect_message_response(value)
        return value.model_copy(update=output_view(value.model_dump(mode="python")))
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "streamed_output":
                result[key] = []  # 日志里的逐片副本由 final_output 代替，避免拆片绕过。
            elif output_view({key: "public-probe"}).get(key) != "public-probe":
                result[key] = "***"
            else:
                result[key] = _event_view(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_event_view(item, depth=depth + 1) for item in value]
    return output_view(value)


async def checked_event_stream(events):
    from app.llm.errors import PROVIDER_TRANSPORT_ERRORS, normalized_provider_error

    pending = []
    chars = 0
    count = 0
    failure = None
    try:
        async for event in events:
            count += 1
            chars += len(str(event))
            if count > 32768 or chars > 2_000_000:
                raise ModelOutputRejected
            # 未完整的 stream 包含可拼接凭证，不做逐块放行；结束事件提供完整结果。
            if not event.get("event", "").endswith("_stream"):
                pending.append(_event_view(event))
    except PROVIDER_TRANSPORT_ERRORS as exc:
        failure = normalized_provider_error(exc)
    if failure:
        raise failure
    for event in pending:
        yield event


async def checked_log_stream(events, *, diff):
    import jsonpatch
    from langchain_core.tracers.log_stream import RunLog, RunLogPatch

    from app.llm.errors import PROVIDER_TRANSPORT_ERRORS, normalized_provider_error

    state = {}
    count = chars = 0
    failure = None
    try:
        async for event in events:
            count += 1
            chars += len(str(event))
            if count > 32768 or chars > 2_000_000:
                raise ModelOutputRejected
            if isinstance(event, RunLog):
                state = event.state
            else:
                state = jsonpatch.apply_patch(state, event.ops)
    except PROVIDER_TRANSPORT_ERRORS as exc:
        failure = normalized_provider_error(exc)
    if failure:
        raise failure
    checked = _event_view(state)
    operation = {"op": "replace", "path": "", "value": checked}
    yield RunLogPatch(operation) if diff else RunLog(operation, state=checked)
