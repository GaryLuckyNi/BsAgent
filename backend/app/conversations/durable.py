"""有序协议日志与数据库内的后台 turn 控制边界。"""

import os
import socket
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.conversations.repository import ConversationNotFoundError
from app.conversations.service import TurnHandle
from app.storage.models import (
    AgentMessage,
    AgentRun,
    AgentRunExecution,
    AgentRunProtocolEvent,
    AgentRunToolReceipt,
    AgentSession,
)

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "rejected", "interrupted"}


class TurnCancelled(BaseException):
    """取消是执行控制信号，不进入业务错误的重试/降级分支。"""


class RunOwnershipLost(BaseException):
    pass


class DurableRunService:
    def __init__(self, db: Session):
        self.db = db

    def prepare(self, *, handle: TurnHandle, payload: dict) -> None:
        if self.db.get(AgentRunExecution, handle.run_id) is not None:
            return
        execution = AgentRunExecution(run_id=handle.run_id, payload=dict(payload), last_sequence=0)
        self.db.add(execution)
        self.db.flush()
        self._event(
            execution,
            "custom",
            {
                "type": "run_started",
                "run_id": handle.run_id,
                "message_id": handle.assistant_message_id,
                "request_id": handle.request_id,
            },
        )

    def claim(self, run_id: str) -> str | None:
        token = uuid4().hex
        result = self.db.execute(
            update(AgentRunExecution)
            .where(
                AgentRunExecution.run_id == run_id,
                AgentRunExecution.owner_token.is_(None),
                AgentRunExecution.producer_done.is_(False),
            )
            .values(
                owner_token=token,
                owner_host=socket.gethostname(),
                owner_pid=os.getpid(),
                heartbeat_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        self.db.expire_all()
        return token if result.rowcount == 1 else None

    def prepare_resume(
        self, *, handle: TurnHandle, execution: AgentRunExecution, payload: dict
    ) -> int:
        execution.phase_start_sequence = execution.last_sequence
        execution.owner_token = None
        execution.producer_done = False
        execution.stop_requested = False
        execution.payload = dict(payload)
        execution.heartbeat_at = None
        self._event(
            execution,
            "custom",
            {
                "type": "run_started",
                "run_id": handle.run_id,
                "message_id": handle.assistant_message_id,
                "request_id": handle.request_id,
            },
        )
        return execution.phase_start_sequence

    def owned(self, run_id: str, owner: str, *, allow_done: bool = False) -> AgentRunExecution:
        if self.db.get_bind().dialect.name == "sqlite":
            # SQLite 忽略 FOR UPDATE，用无值变更的写入获得同等事务互斥。
            self.db.execute(
                update(AgentRunExecution)
                .where(
                    AgentRunExecution.run_id == run_id,
                    AgentRunExecution.owner_token == owner,
                )
                .values(owner_token=AgentRunExecution.owner_token)
                .execution_options(synchronize_session=False)
            )
        execution = self.db.scalar(
            select(AgentRunExecution)
            .where(
                AgentRunExecution.run_id == run_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            execution is None
            or execution.owner_token != owner
            or (execution.producer_done and not allow_done)
        ):
            self.db.rollback()
            raise RunOwnershipLost(run_id)
        return execution

    def guard(self, run_id: str, owner: str) -> None:
        execution = self.owned(run_id, owner)
        if execution.stop_requested:
            self.db.rollback()
            raise TurnCancelled(run_id)
        run = self.db.get(AgentRun, run_id, populate_existing=True)
        if run.status in {"completed", "failed", "cancelled", "rejected", "interrupted"}:
            return
        session = self.db.get(AgentSession, run.session_key, populate_existing=True)
        if session.active_run_id != run_id:
            self.db.rollback()
            raise RunOwnershipLost(run_id)

    def append(self, run_id: str, owner: str, event: str, data: dict | list) -> None:
        execution = self.owned(run_id, owner)
        self._event(execution, event, data)
        if event == "messages" and isinstance(data, list) and data:
            chunk = data[0]
            content = chunk.get("content", "") if isinstance(chunk, dict) else ""
            if isinstance(content, str) and content:
                self.db.execute(
                    update(AgentMessage)
                    .where(
                        AgentMessage.run_id == run_id,
                        AgentMessage.role == "assistant",
                        AgentMessage.status.in_(("pending", "streaming")),
                    )
                    .values(content=AgentMessage.content + content, status="streaming")
                    .execution_options(synchronize_session=False)
                )
        self.db.flush()

    def events(self, run_id: str, *, after: int) -> list[AgentRunProtocolEvent]:
        return list(
            self.db.scalars(
                select(AgentRunProtocolEvent)
                .where(
                    AgentRunProtocolEvent.run_id == run_id,
                    AgentRunProtocolEvent.sequence > after,
                )
                .order_by(AgentRunProtocolEvent.sequence)
                .limit(200)
            )
        )

    def request_stop(self, run_id: str) -> str:
        # 与完成路径使用相同锁顺序，先 execution 后 run。
        execution = self.db.scalar(
            select(AgentRunExecution)
            .where(
                AgentRunExecution.run_id == run_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        run = self.db.scalar(
            select(AgentRun)
            .where(AgentRun.run_id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise ConversationNotFoundError("run_not_found")
        if run.status in {"completed", "failed", "cancelled", "rejected"}:
            return run.status
        if execution is None:
            # 旧入口或正在审批恢复的执行没有后台所有权，不能冒然释放。
            raise ValueError("run_stop_not_supported")
        if execution.producer_done and run.status != "interrupted":
            raise ValueError("run_stop_not_supported")
        execution.stop_requested = True
        if run.status == "interrupted" and execution.producer_done:
            execution.producer_done = False
            self.db.flush()
            self.finish_cancelled(run_id, execution.owner_token)
            return "cancelled"
        run.status = "stop_requested"
        self.db.flush()
        return "stop_requested"

    def finish_cancelled(self, run_id: str, owner: str) -> None:
        execution = self.owned(run_id, owner, allow_done=True)
        run = self.db.get(AgentRun, run_id, populate_existing=True)
        if run.status in {"completed", "failed", "cancelled", "rejected"}:
            return
        execution.stop_requested = True
        unresolved = self.db.scalar(
            select(AgentRunToolReceipt.receipt_id)
            .where(
                AgentRunToolReceipt.run_id == run_id,
                AgentRunToolReceipt.status == "running",
            )
            .limit(1)
        )
        if unresolved:
            # 未知副作用必须核实；不得释放会话让冲突写入进入。
            run.status = "stop_requested"
            return
        run.status = "cancelled"
        self.db.execute(
            update(AgentMessage)
            .where(
                AgentMessage.run_id == run_id,
                AgentMessage.role == "assistant",
                AgentMessage.status.in_(("pending", "streaming", "interrupted")),
            )
            .values(status="cancelled", completed_at=datetime.now(UTC))
        )
        self.db.execute(
            update(AgentSession)
            .where(
                AgentSession.session_key == run.session_key,
                AgentSession.active_run_id == run_id,
            )
            .values(active_run_id=None)
        )
        self.db.flush()
        self.publish_status(run_id, owner)

    def has_running_tools(self, run_id: str) -> bool:
        return (
            self.db.scalar(
                select(AgentRunToolReceipt.receipt_id)
                .where(
                    AgentRunToolReceipt.run_id == run_id,
                    AgentRunToolReceipt.status == "running",
                )
                .limit(1)
            )
            is not None
        )

    def defer_until_tools_settled(self, run_id: str, owner: str) -> None:
        execution = self.owned(run_id, owner)
        execution.stop_requested = True
        run = self.db.get(AgentRun, run_id, populate_existing=True)
        run.status = "stop_requested"
        self.db.flush()

    def publish_status(self, run_id: str, owner: str) -> None:
        execution = self.owned(run_id, owner)
        run = self.db.get(AgentRun, run_id, populate_existing=True)
        self._event(
            execution,
            "custom",
            {
                "type": "run_status",
                "run_id": run_id,
                "status": run.status,
            },
        )
        execution.producer_done = run.status in TERMINAL_STATUSES
        execution.heartbeat_at = datetime.now(UTC)
        self.db.flush()

    def _event(self, execution: AgentRunExecution, event: str, data: dict | list) -> None:
        execution.last_sequence += 1
        self.db.add(
            AgentRunProtocolEvent(
                run_id=execution.run_id, sequence=execution.last_sequence, event=event, data=data
            )
        )
        self.db.flush()
