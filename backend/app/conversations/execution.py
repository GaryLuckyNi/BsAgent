"""生产者独立于 HTTP 订阅，取消在节点/工具边界协作完成。"""

import json
import logging
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
from app.storage.models import AgentRun, AgentRunExecution, AgentRunToolReceipt

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
            execution = db.get(AgentRunExecution, control.run_id)
            if (
                execution is None
                or execution.producer_done
                or execution.owner_token != control.owner
            ):
                forget_finished_producer(control.run_id, control.owner)
    except Exception:
        logger.exception("turn_exit_status_unavailable run_id=%s", control.run_id)


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

    def ensure_tool_effects_resolved(self, *, risk_level, db=None, call_id=None):
        if risk_level == "low":
            return
        if db is None:
            with self.session_factory() as session:
                return self.ensure_tool_effects_resolved(
                    risk_level=risk_level, db=session, call_id=call_id
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
                AgentRunToolReceipt.risk_level.in_(("medium", "high")),
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
                    AgentRunToolReceipt.risk_level.in_(("medium", "high")),
                    AgentRunToolReceipt.status == "settled",
                    AgentRunToolReceipt.result_summary["status"].as_string() == "success",
                )
                .limit(1)
            )
            if succeeded:
                raise ToolCallAlreadyExecuted("tool_call_already_executed")

    def tool_started(self, request, *, risk_level, capability_scope=None):
        receipt_id = uuid4().hex
        with self.session_factory() as db:
            self.guard(db)
            self.ensure_tool_effects_resolved(risk_level=risk_level, db=db, call_id=request.call_id)
            db.add(
                AgentRunToolReceipt(
                    receipt_id=receipt_id,
                    run_id=self.run_id,
                    owner_token=self.owner,
                    tool_name=request.tool_name,
                    call_id=request.call_id,
                    risk_level=risk_level,
                    status="running",
                    result_summary=({"_capability_scope": capability_scope} if capability_scope else {}),
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
            summary = {k: v for k, v in summary.items() if k not in {"_invocation", "_capability_scope"}}
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


def produce_frames(*, session_factory, run_id, frames_factory):
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
                stream = frames_factory(db)
                for frame in stream:
                    event, data = parse_frame(frame)
                    if event == "custom" and isinstance(data, dict):
                        data = {**data, "run_id": run_id}
                    # 订阅事件使用独立事务；请求 DB session 从未跨线程传递。
                    with session_factory() as event_db:
                        DurableRunService(event_db).append(run_id, owner, event, data)
                        event_db.commit()
                    control.guard()
                _finish_producer(control, error_code="agent_stream_incomplete")
            except TurnCancelled:
                db.rollback()
                with session_factory() as event_db:
                    DurableRunService(event_db).finish_cancelled(run_id, owner)
                    event_db.commit()
            except RunOwnershipLost:
                db.rollback()
                logger.warning("turn_owner_fenced run_id=%s", run_id)
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
    cursor = after
    last_heartbeat = monotonic()
    while True:
        with session_factory() as db:
            execution = db.get(AgentRunExecution, run_id)
            done = execution is None or execution.producer_done
            events = DurableRunService(db).events(run_id, after=cursor)
            # 在连接持有期间不保留事务/连接或生产者资源。
            frames = [(event.sequence, event.event, event.data) for event in events]
        for sequence, event, data in frames:
            cursor = sequence
            yield f"event: {event}\nid: {sequence}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        if done and len(frames) < 200:
            return
        if not frames:
            if monotonic() - last_heartbeat >= 1:
                # 注释帧不占事件序号；定期退出同步 next 以让 ASGI 响应断线取消。
                last_heartbeat = monotonic()
                yield ": heartbeat\n\n"
            sleep(0.1)
