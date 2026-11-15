"""生产者独立于 HTTP 订阅，取消在节点/工具边界协作完成。"""

import json
import logging
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from threading import Event, Lock, Thread
from time import monotonic, sleep
from typing import Literal
from uuid import uuid4

from sqlalchemy import select, update

from app.conversations.durable import DurableRunService, RunOwnershipLost, TurnCancelled
from app.schemas.tools import ToolCallResult
from app.security.run_authorization import RunAuthorizationDenied, RunAuthorizationUnavailable
from app.storage.models import (
    AgentMessage,
    AgentRun,
    AgentRunExecution,
    AgentRunProtocolEvent,
    AgentRunToolReceipt,
)

logger = logging.getLogger(__name__)
_finished_owners: set[tuple[str, str]] = set()
_finished_owners_lock = Lock()
_pending_tool_results: dict[str, tuple[object, dict, bool, str | None]] = {}


def producer_finished_locally(run_id, owner):
    with _finished_owners_lock:
        return (run_id, owner) in _finished_owners


def forget_finished_producer(run_id, owner):
    with _finished_owners_lock:
        _finished_owners.discard((run_id, owner))


def note_producer_finished(control):
    # 仅在 generator 已关闭、生产者已退出后记录；活线程/过期心跳不能生成此证据。
    with _finished_owners_lock:
        _finished_owners.add((control.run_id, control.owner))
    try:
        with control.session_factory() as db:
            release_terminal_catalog_snapshot(db, control.run_id)
            execution = db.get(AgentRunExecution, control.run_id)
            if (
                execution is None
                or execution.producer_done
                or execution.owner_token != control.owner
            ):
                forget_finished_producer(control.run_id, control.owner)
    except Exception:
        logger.exception("turn_exit_status_unavailable run_id=%s", control.run_id)


def release_terminal_catalog_snapshot(db, run_id):
    """在业务状态提交后释放本进程的 run 快照；有效中断和未决取消仍保留。"""
    try:
        run = db.get(AgentRun, run_id, populate_existing=True)
        if run is None or run.status not in {"completed", "failed", "cancelled", "rejected"}:
            return
        assistant = db.scalar(select(AgentMessage).where(
            AgentMessage.tenant_id == run.tenant_id, AgentMessage.run_id == run_id,
            AgentMessage.role == "assistant",
        ))
        if assistant is not None:
            from app.graph.state import build_checkpoint_thread_id
            from app.tools.default_registry import get_default_tool_catalog_runtime

            get_default_tool_catalog_runtime().release_run_snapshot(build_checkpoint_thread_id(
                tenant_id=run.tenant_id, session_key=run.session_key, request_id=assistant.request_id,
            ))
    except Exception:  # 清理失败不能改写已提交的业务结果
        logger.exception("run_catalog_release_failed run_id=%s", run_id)


def retry_pending_tool_results(engine, *, run_id=None):
    """数据库短故障时只重试已返回结果的落库，绝不重新执行外部工具。"""
    with _finished_owners_lock:
        pending = list(_pending_tool_results.items())
    persisted = 0
    for receipt_id, (control, summary, uncertain, result_digest) in pending:
        if getattr(control.session_factory, "kw", {}).get("bind") is not engine:
            continue
        if run_id is not None and control.run_id != run_id:
            continue
        try:
            control._persist_tool_result(receipt_id, summary, uncertain, result_digest)
            persisted += 1
        except RunOwnershipLost:
            with _finished_owners_lock:
                _pending_tool_results.pop(receipt_id, None)
            logger.warning("tool_receipt_retry_fenced run_id=%s", control.run_id)
        except Exception:
            logger.exception("tool_receipt_retry_failed run_id=%s", control.run_id)
    return persisted


class UnresolvedToolEffect(RuntimeError):
    code = "tool_side_effects_unresolved"


class ToolCallAlreadyExecuted(UnresolvedToolEffect):
    code = "tool_call_already_executed"


@dataclass(frozen=True)
class SearchInvocationClaim:
    disposition: Literal["execute", "reuse", "blocked", "exhausted"]
    receipt_id: str | None = None
    result: ToolCallResult | None = None


def _result_digest(value) -> str | None:
    try:
        result = ToolCallResult.model_validate(value)
        canonical = json.dumps(result.model_dump(mode="json"), sort_keys=True,
                               ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        return None
    return sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ExecutionControl:
    session_factory: object
    run_id: str
    owner: str

    def guard(self, db=None):
        if db is not None:
            DurableRunService(db).guard(self.run_id, self.owner)
            return
        with self.session_factory() as session:
            DurableRunService(session).guard(self.run_id, self.owner)

    def ensure_tool_effects_resolved(self, *, risk_level, effect_type="unknown", db=None, call_id=None):
        if effect_type != "write":
            return
        if db is None:
            with self.session_factory() as session:
                return self.ensure_tool_effects_resolved(
                    risk_level=risk_level, effect_type=effect_type, db=session, call_id=call_id
                )
        run = db.get(AgentRun, self.run_id)
        unknown = db.scalar(
            select(AgentRunToolReceipt.receipt_id)
            .join(
                AgentRun,
                AgentRun.run_id == AgentRunToolReceipt.run_id,
            )
            .where(
                AgentRun.session_key == run.session_key,
                AgentRun.tenant_id == run.tenant_id,
                AgentRunToolReceipt.result_summary["_effect_type"].as_string() == "write",
                AgentRunToolReceipt.status.in_(("unknown", "running")),
            )
            .limit(1)
        )
        if unknown:
            raise UnresolvedToolEffect("tool_side_effects_unresolved")
        if call_id:
            succeeded = db.scalar(
                select(AgentRunToolReceipt.receipt_id)
                .where(
                    AgentRunToolReceipt.run_id == self.run_id,
                    AgentRunToolReceipt.call_id == call_id,
                    AgentRunToolReceipt.result_summary["_effect_type"].as_string() == "write",
                    AgentRunToolReceipt.status == "settled",
                    AgentRunToolReceipt.result_summary["status"].as_string() == "success",
                )
                .limit(1)
            )
            if succeeded:
                raise ToolCallAlreadyExecuted("tool_call_already_executed")

    def tool_started(self, request, *, risk_level, effect_type="unknown", capability_scope=None):
        receipt_id = uuid4().hex
        with self.session_factory() as db:
            self.guard(db)
            self.ensure_tool_effects_resolved(
                risk_level=risk_level, effect_type=effect_type, db=db, call_id=request.call_id,
            )
            db.add(
                AgentRunToolReceipt(
                    receipt_id=receipt_id,
                    run_id=self.run_id,
                    owner_token=self.owner,
                    tool_name=request.tool_name,
                    call_id=request.call_id,
                    risk_level=risk_level,
                    status="running",
                    result_summary={
                        "_effect_type": effect_type,
                        **({"_capability_scope": capability_scope} if capability_scope else {}),
                    },
                )
            )
            db.commit()
        return receipt_id

    def capability_observations(self, *, tool_name=None):
        from app.tools.runtime_capabilities import observation_from_receipt

        with self.session_factory() as db:
            self.guard(db)
            query = select(AgentRunToolReceipt).where(
                AgentRunToolReceipt.run_id == self.run_id,
                AgentRunToolReceipt.status == "settled",
            )
            if tool_name is not None:
                query = query.where(AgentRunToolReceipt.tool_name == tool_name)
            return tuple(observation for receipt in db.scalars(query)
                         if (observation := observation_from_receipt(receipt)) is not None)

    def claim_search_invocation(self, request, *, binding_digest, budget_limit, existing_call_ids):
        """同 Run 锁内预留真实搜索；未决调用不能因低风险而被自动重发。"""
        from app.tools.fingerprints import tool_call_fingerprint

        if (request.tool_name != "web_search" or not request.call_id or not binding_digest
                or isinstance(budget_limit, bool) or budget_limit < 1):
            raise ValueError("search_invocation_invalid")
        identity = {"version": "search-invocation-v1", "binding_digest": binding_digest,
                    "fingerprint": tool_call_fingerprint(request)}
        with self.session_factory() as db:
            self.guard(db)
            rows = list(db.scalars(select(AgentRunToolReceipt).where(
                AgentRunToolReceipt.run_id == self.run_id,
                AgentRunToolReceipt.call_id == request.call_id,
            )))
            if rows:
                if len(rows) != 1:
                    return SearchInvocationClaim("blocked")
                receipt = rows[0]
                summary = receipt.result_summary
                stored = summary.get("_invocation", {}) if isinstance(summary, dict) else {}
                if (receipt.tool_name != "web_search" or receipt.status != "settled"
                        or not isinstance(stored, dict)
                        or any(stored.get(k) != v for k, v in identity.items())
                        or not stored.get("result_digest")
                        or _result_digest(summary) != stored["result_digest"]):
                    return SearchInvocationClaim("blocked", receipt.receipt_id)
                return SearchInvocationClaim(
                    "reuse", receipt.receipt_id, ToolCallResult.model_validate(summary),
                )
            used = set(existing_call_ids)
            for receipt in db.scalars(select(AgentRunToolReceipt).where(
                AgentRunToolReceipt.run_id == self.run_id,
                AgentRunToolReceipt.tool_name == "web_search",
            )):
                used.add(receipt.call_id or f"legacy:{receipt.receipt_id}")
            if len(used) >= budget_limit:
                return SearchInvocationClaim("exhausted")
            receipt_id = uuid4().hex
            db.add(AgentRunToolReceipt(
                receipt_id=receipt_id, run_id=self.run_id, owner_token=self.owner,
                tool_name="web_search", call_id=request.call_id, risk_level="low",
                status="running", result_summary={"_invocation": identity},
            ))
            db.commit()
            return SearchInvocationClaim("execute", receipt_id)

    def tool_finished(self, receipt_id, result, *, uncertain=False):
        from app.tools.redaction import sanitize_untrusted_details

        summary = sanitize_untrusted_details(result)
        # 保留清理前的完整性指纹；重试落库也不得把截断摘要升级为完整结果。
        result_digest = _result_digest(result)
        with _finished_owners_lock:
            _pending_tool_results[receipt_id] = (self, summary, uncertain, result_digest)
        self._persist_tool_result(receipt_id, summary, uncertain, result_digest)

    def _persist_tool_result(self, receipt_id, summary, uncertain, result_digest):
        with self.session_factory() as db:
            # 先保存结果，再观察取消；不允许因为 stop 丢弃已执行工具结果。
            DurableRunService(db).owned(self.run_id, self.owner, allow_done=True)
            receipt = db.get(AgentRunToolReceipt, receipt_id)
            # 调用身份只接受预留时落盘的服务端元数据，不接受工具返回中的同名字段。
            summary = {k: v for k, v in summary.items() if k not in {"_invocation", "_capability_scope", "_effect_type"}}
            if receipt is not None and "_effect_type" in receipt.result_summary:
                summary["_effect_type"] = receipt.result_summary["_effect_type"]
                if summary["_effect_type"] == "read_only":
                    uncertain = False
            if receipt is not None and isinstance(receipt.result_summary.get("_capability_scope"), dict):
                summary["_capability_scope"] = receipt.result_summary["_capability_scope"]
            if receipt is not None and isinstance(receipt.result_summary.get("_invocation"), dict):
                summary["_invocation"] = {
                    **receipt.result_summary["_invocation"], "result_digest": result_digest,
                }
            db.execute(
                update(AgentRunToolReceipt)
                .where(
                    AgentRunToolReceipt.receipt_id == receipt_id,
                    AgentRunToolReceipt.owner_token == self.owner,
                )
                .values(
                    status="unknown" if uncertain else "settled",
                    result_summary=summary,
                    completed_at=datetime.now(UTC),
                )
            )
            execution = db.get(AgentRunExecution, self.run_id)
            if execution.stop_requested and not execution.producer_done:
                DurableRunService(db).finish_cancelled(self.run_id, self.owner)
            db.commit()
            if execution.producer_done:
                release_terminal_catalog_snapshot(db, self.run_id)
                forget_finished_producer(self.run_id, self.owner)
        with _finished_owners_lock:
            _pending_tool_results.pop(receipt_id, None)


current_execution: ContextVar[ExecutionControl | None] = ContextVar("turn_execution", default=None)


def check_execution_cancelled(db=None):
    control = current_execution.get()
    if control is not None:
        control.guard(db)


@contextmanager
def execution_control(control):
    token = current_execution.set(control)
    try:
        yield
    finally:
        current_execution.reset(token)


def _heartbeat(control, stopped):
    while not stopped.wait(5):
        try:
            with control.session_factory() as db:
                db.execute(
                    update(AgentRunExecution)
                    .where(
                        AgentRunExecution.run_id == control.run_id,
                        AgentRunExecution.owner_token == control.owner,
                        AgentRunExecution.producer_done.is_(False),
                    )
                    .values(heartbeat_at=datetime.now(UTC))
                )
                db.commit()
        except Exception:
            logger.exception("turn_heartbeat_failed run_id=%s", control.run_id)


@contextmanager
def execution_heartbeat(control):
    stopped = Event()
    heartbeat = Thread(target=_heartbeat, args=(control, stopped), daemon=True)
    heartbeat.start()
    try:
        yield
    finally:
        stopped.set()
        heartbeat.join(timeout=1)


class _PublicEventTiming:
    """只测量对外进展开始到对外正文开始，不读取模型内部生成时刻。"""

    def __init__(self, *, started_at=None, answer_started=False, clock: Callable[[], datetime] | None = None):
        self._started_at = started_at
        if started_at is not None and started_at.tzinfo is None:
            self._started_at = started_at.replace(tzinfo=UTC)
        self._answer_started = answer_started
        self._clock = clock or (lambda: datetime.now(UTC))

    def observe(self, event, data):
        if self._answer_started:
            return None
        if (event == "custom" and isinstance(data, dict) and data.get("type") == "progress"
                and self._started_at is None and isinstance(data.get("message"), str) and data["message"].strip()):
            self._started_at = self._clock()
        if event == "messages" and isinstance(data, list) and data:
            content = data[0].get("content") if isinstance(data[0], dict) else None
            if isinstance(content, str) and content.strip():
                self._answer_started = True
                if self._started_at is not None:
                    return {"type": "answer_started", "elapsed_ms": max(
                        0, round((self._clock() - self._started_at).total_seconds() * 1000),
                    )}
        return None


def _restore_public_event_timing(db, run_id):
    execution = db.get(AgentRunExecution, run_id)
    if not execution.phase_start_sequence:
        return _PublicEventTiming()
    progress = db.scalar(select(AgentRunProtocolEvent).where(
        AgentRunProtocolEvent.run_id == run_id,
        AgentRunProtocolEvent.event == "custom",
        AgentRunProtocolEvent.data["type"].as_string() == "progress",
    ).order_by(AgentRunProtocolEvent.sequence).limit(1))
    body = db.scalar(select(AgentRunProtocolEvent.sequence).where(
        AgentRunProtocolEvent.run_id == run_id, AgentRunProtocolEvent.event == "messages",
    ).limit(1))
    return _PublicEventTiming(started_at=progress.created_at if progress is not None else None,
                              answer_started=body is not None)


def produce_frames(*, session_factory, run_id, frames_factory):
    from app.security.protocol_replay import CheckedProtocolWriter
    content_writer = CheckedProtocolWriter()
    with session_factory() as db:
        owner = DurableRunService(db).claim(run_id)
        db.commit()
    if owner is None:
        return
    control = ExecutionControl(session_factory, run_id, owner)
    stopped = Event()
    heartbeat = Thread(target=_heartbeat, args=(control, stopped), daemon=True)
    heartbeat.start()
    try:
        with execution_control(control), session_factory() as db:
            stream = None
            try:
                control.guard()
                timing = _restore_public_event_timing(db, run_id)
                stream = frames_factory(db)
                for frame in stream:
                    event, data = parse_frame(frame)
                    if event == "custom" and isinstance(data, dict):
                        data = {**data, "run_id": run_id}
                    # 订阅事件使用独立事务；请求 DB session 从未跨线程传递。
                    with session_factory() as event_db:
                        service = DurableRunService(event_db)
                        for safe_event, safe_data, checked in content_writer.project(event, data):
                            answer_timing = timing.observe(safe_event, safe_data)
                            if answer_timing is not None:
                                service.append(run_id, owner, "custom", {**answer_timing, "run_id": run_id})
                            service.append(run_id, owner, safe_event, safe_data, content_checked=checked)
                        event_db.commit()
                    control.guard()
                with session_factory() as event_db:
                    service = DurableRunService(event_db)
                    for safe_event, safe_data, checked in content_writer.finish():
                        service.append(run_id, owner, safe_event, safe_data, content_checked=checked)
                    event_db.commit()
                _finish_producer(control, error_code="agent_stream_incomplete")
            except TurnCancelled:
                db.rollback()
                with session_factory() as event_db:
                    service = DurableRunService(event_db)
                    # 停止后只封口已接收尾部，不再推进源或执行工具。
                    for safe_event, safe_data, checked in content_writer.finish():
                        service.append(run_id, owner, safe_event, safe_data, content_checked=checked)
                    service.finish_cancelled(run_id, owner)
                    event_db.commit()
            except RunOwnershipLost:
                db.rollback()
                logger.warning("turn_owner_fenced run_id=%s", run_id)
            except (RunAuthorizationDenied, RunAuthorizationUnavailable) as exc:
                db.rollback()
                _finish_producer(control, error_code=exc.code)
            except Exception:
                db.rollback()
                logger.exception("turn_producer_failed run_id=%s", run_id)
                _finish_producer(control, error_code="agent_execution_failed")
            finally:
                if stream is not None:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        try:
                            close()
                        except (TurnCancelled, RunOwnershipLost):
                            pass
    finally:
        stopped.set()
        heartbeat.join(timeout=1)
        note_producer_finished(control)


def _finish_producer(control, *, error_code):
    from app.conversations.service import ConversationService

    retry_pending_tool_results(control.session_factory.kw["bind"], run_id=control.run_id)
    # 完成/stop/失败同锁串行化。控制信号不再进入旧 generator 的错误降级路径。
    with execution_control(None), control.session_factory() as db:
        service = DurableRunService(db)
        execution = service.owned(control.run_id, control.owner, allow_done=True)
        if execution.producer_done:
            return
        run = db.get(AgentRun, control.run_id, populate_existing=True)
        if service.has_running_tools(control.run_id):
            service.defer_until_tools_settled(control.run_id, control.owner)
            db.commit()
            return
        if execution.stop_requested or run.status == "stop_requested":
            service.finish_cancelled(control.run_id, control.owner)
        else:
            if run.status == "running":
                ConversationService(db).fail_turn(
                    tenant_id=run.tenant_id, run_id=run.run_id, error_code=error_code
                )
                service.append(
                    run.run_id, control.owner, "error", {"code": error_code, "run_id": run.run_id}
                )
            service.publish_status(run.run_id, control.owner)
        db.commit()


def parse_frame(frame):
    event, data = "message", {}
    for line in frame.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data = json.loads(line[5:].strip())
    return event, data


def subscribe_events(*, session_factory, run_id, after=0):
    from app.security.protocol_replay import (
        completed_replay_bodies,
        is_checked_stream,
        public_protocol_data,
    )

    cursor = after
    scan_cursor = after
    waiting_for_body = False
    bodies = None
    last_heartbeat = monotonic()
    while True:
        with session_factory() as db:
            execution = db.get(AgentRunExecution, run_id)
            done = execution is None or execution.producer_done
            if done:
                through = execution.last_sequence if execution is not None else db.scalar(
                    select(AgentRunProtocolEvent.sequence).where(
                        AgentRunProtocolEvent.run_id == run_id,
                    ).order_by(AgentRunProtocolEvent.sequence.desc()).limit(1)
                ) or 0
                if bodies is None or bodies.through < through:
                    bodies = completed_replay_bodies(db, run_id, through)
            if waiting_for_body and done:
                scan_cursor = cursor
                waiting_for_body = False
            events = DurableRunService(db).events(run_id, after=scan_cursor)
            # 在连接持有期间不保留事务/连接或生产者资源。
            frames = [(event.sequence, event.event, event.data) for event in events]
        for sequence, event, data in frames:
            scan_cursor = sequence
            if event == "messages" and not is_checked_stream(data) and (bodies is None or sequence > bodies.through):
                waiting_for_body = True
            if waiting_for_body:
                # 进度不推进重连游标，避免断线后跳过尚未检查/发送的正文。
                if event == "custom" and isinstance(data, dict) and data.get("type") in {"progress", "answer_sources"}:
                    safe = public_protocol_data(event, data)
                    yield f"event: {event}\ndata: {json.dumps(safe, ensure_ascii=False)}\n\n"
                continue
            cursor = sequence
            safe = public_protocol_data(event, data, sequence=sequence, bodies=bodies)
            yield f"event: {event}\nid: {sequence}\ndata: {json.dumps(safe, ensure_ascii=False)}\n\n"
        if done and len(frames) < 200 and not waiting_for_body:
            return
        if not frames:
            if monotonic() - last_heartbeat >= 1:
                # 注释帧不占事件序号；定期退出同步 next 以让 ASGI 响应断线取消。
                last_heartbeat = monotonic()
                yield ": heartbeat\n\n"
            sleep(0.1)
