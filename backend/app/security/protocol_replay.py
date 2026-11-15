"""持久化公共协议的只读安全副本；正文完整重建后检查。"""

from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import and_, or_, select

from app.security.public_output import (
    WITHHELD_PUBLIC_TEXT,
    public_data,
    public_progress,
    public_sources,
    public_text,
)
from app.storage.models import AgentMessage, AgentRun, AgentRunExecution, AgentRunProtocolEvent

MAX_REPLAY_EVENTS = 20_000
MAX_REPLAY_BODY_CHARS = 2_000_000


def is_checked_stream(data):
    from app.security.model_output import _output_policy
    return (
        isinstance(data, list) and len(data) > 1 and isinstance(data[1], dict)
        and data[1].get("security_content_policy") == _output_policy().version
    )


class CheckedProtocolWriter:
    """生产者生成可持久化的安全增量；外部元数据不能授予已检查状态。"""

    def __init__(self):
        from app.security.model_output import IncrementalTextGuard
        self.guard = IncrementalTextGuard()
        self.template = None

    def _message(self, text):
        if not text or self.template is None:
            return []
        return [("messages", [{**self.template[0], "content": text}, self.template[1]], True)]

    def project(self, event, data):
        if event == "messages":
            from app.security.model_output import ModelOutputRejected
            if not isinstance(data, list) or not data or not isinstance(data[0], dict):
                raise ModelOutputRejected
            self.template = [data[0], data[1] if len(data) > 1 and isinstance(data[1], dict) else {}]
            return self._message(self.guard.feed(data[0].get("content")))
        prefix = []
        if event == "custom" and isinstance(data, dict) and data.get("type") in {
            "final", "run_status", "run_error", "run_completed", "run_started"
        }:
            prefix = self.finish()
            from app.security.model_output import IncrementalTextGuard
            self.guard = IncrementalTextGuard()
        return [*prefix, (event, data, False)]

    def finish(self):
        return self._message(self.guard.finish())

_CONTROLS = (
    "type",
    "id",
    "run_id",
    "message_id",
    "user_message_id",
    "request_id",
    "status",
    "trace_id",
    "approval_status",
    "approval_ids",
    "cited_evidence_ids",
    "response_type",
    "business_error_code",
    "code",
    "node",
    "elapsed_ms",
)


def sealed_message_condition():
    open_execution = (
        select(AgentRunExecution.run_id)
        .where(
            AgentRunExecution.run_id == AgentMessage.run_id,
            AgentRunExecution.producer_done.is_(False),
        )
        .correlate(AgentMessage)
        .exists()
    )
    return or_(
        AgentMessage.role != "assistant",
        and_(AgentMessage.status.not_in(("pending", "streaming")), ~open_execution),
    )


def public_message_snapshots(db, records):
    """正文、消息状态和生产者封口同一 SQL 快照；不复用 ORM 的旧正文。"""
    if not records:
        return []
    attributes = {
        prop.key: getattr(AgentMessage, prop.key) for prop in AgentMessage.__mapper__.column_attrs
    }
    rows = db.execute(
        select(
            *(column.label(name) for name, column in attributes.items()),
            AgentRunExecution.producer_done.label("producer_done"),
            AgentRunExecution.last_sequence.label("last_event_sequence"),
            AgentRunExecution.phase_start_sequence.label("replay_after"),
            AgentRun.status.label("run_status"),
        )
        .join(AgentRun, AgentRun.run_id == AgentMessage.run_id)
        .outerjoin(
            AgentRunExecution,
            AgentRunExecution.run_id == AgentMessage.run_id,
        )
        .where(AgentMessage.message_id.in_([record.message_id for record in records]))
    ).mappings()
    snapshots = {}
    for row in rows:
        snapshot = SimpleNamespace(**row)
        sealed = snapshot.role != "assistant" or (
            snapshot.status not in {"pending", "streaming"} and snapshot.producer_done is not False
        )
        snapshot.content = public_text(snapshot.content) if sealed else ""
        snapshots[snapshot.message_id] = snapshot
    return [snapshots[record.message_id] for record in records if record.message_id in snapshots]


@dataclass(frozen=True)
class ReplayBodies:
    through: int
    contents: dict[int, str]


def completed_replay_bodies(db, run_id, through):
    """从头读取检查上下文，不能从客户端游标截断凭证的前半段。"""
    groups, count, chars, cursor, phase = {}, 0, 0, 0, 0
    while cursor < through:
        rows = list(
            db.scalars(
                select(AgentRunProtocolEvent)
                .where(
                    AgentRunProtocolEvent.run_id == run_id,
                    AgentRunProtocolEvent.sequence > cursor,
                    AgentRunProtocolEvent.sequence <= through,
                )
                .order_by(AgentRunProtocolEvent.sequence)
                .limit(200)
            )
        )
        if not rows:
            break
        for row in rows:
            cursor = row.sequence
            count += 1
            if count > MAX_REPLAY_EVENTS:
                return ReplayBodies(through, {})
            if (
                row.event == "custom"
                and isinstance(row.data, dict)
                and row.data.get("type") == "run_started"
            ):
                phase = row.sequence
            if row.event != "messages":
                continue
            data = row.data
            if not isinstance(data, list) or not data or not isinstance(data[0], dict):
                return ReplayBodies(through, {})
            chunk = data[0]
            identity, content = chunk.get("id"), chunk.get("content")
            if not isinstance(identity, str) or not isinstance(content, str):
                return ReplayBodies(through, {})
            chars += len(content)
            if chars > MAX_REPLAY_BODY_CHARS:
                return ReplayBodies(through, {})
            # 恢复客户端按阶段拼接正文，不能用不同消息 ID 拆开凭证检查。
            groups.setdefault(phase, []).append((row.sequence, content))
    contents = {}
    for chunks in groups.values():
        text = public_text("".join(content for _, content in chunks))
        offset = 0
        for index, (sequence, original) in enumerate(chunks):
            end = offset + len(original)
            contents[sequence] = text[offset:end] if index < len(chunks) - 1 else text[offset:]
            offset = end
    return ReplayBodies(through, contents)


def _safe_object(data):
    safe = public_data(data)
    if not isinstance(safe, dict) or safe.get("content_withheld") is True:
        # 检查故障不伪造终态；控制字段仅来自已持久化宿主协议。
        safe = {key: data[key] for key in _CONTROLS if key in data}
        safe["message"] = WITHHELD_PUBLIC_TEXT
    return safe


def public_protocol_data(event, data, *, sequence=None, bodies=None):
    if event == "messages":
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return []
        chunk = _safe_object({key: value for key, value in data[0].items() if key != "content"})
        chunk["content"] = (
            public_text(data[0].get("content", "")) if is_checked_stream(data) else
            bodies.contents.get(sequence, WITHHELD_PUBLIC_TEXT)
            if bodies is not None and sequence <= bodies.through
            else WITHHELD_PUBLIC_TEXT
        )
        return [chunk, public_data(data[1]) if len(data) > 1 else {}]
    if not isinstance(data, dict):
        return public_data(data)
    safe = _safe_object(data)
    if event == "custom":
        if data.get("type") == "progress":
            return public_progress(safe)
        if data.get("type") == "answer_sources":
            safe["sources"] = public_sources(data.get("sources"))
        if data.get("type") == "final" and isinstance(data.get("response"), dict):
            response = data["response"]
            safe["response"] = _safe_object(response)
            safe["response"]["content"] = public_text(response.get("content", ""))
            if "sources" in response:
                safe["response"]["sources"] = public_sources(response["sources"])
    return safe
