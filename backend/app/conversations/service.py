import hashlib
import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.conversations.repository import (
    ConversationConflictError,
    ConversationIntegrityError,
    ConversationNotFoundError,
    ConversationRepository,
    SessionBusyError,
)
from app.core.config import settings
from app.storage.models import AgentMessage, AgentRun, AgentSession

_ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")


class ConversationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attachment_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=settings.max_message_attachments,
    )
    content_type: Literal["text"] | None = None

    @field_validator("attachment_ids")
    @classmethod
    def validate_attachment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(_ATTACHMENT_ID.fullmatch(attachment_id) is None for attachment_id in value):
            raise ValueError("invalid_attachment_id")
        return value


class BeginTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    user_content: str
    metadata: ConversationMetadata = Field(default_factory=ConversationMetadata)


class TurnHandle(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_key: str
    session_id: str
    run_id: str
    request_id: str
    trace_id: str
    user_message_id: str
    assistant_message_id: str
    replayed: bool = False


class ConversationService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repository = ConversationRepository(session)

    def delete_session(self, *, tenant_id: str, user_id: str, session_id: str) -> None:
        from app.conversations.durable import DurableRunService

        for _ in range(3):
            record = self._repository.get_owned_session(
                tenant_id=tenant_id, user_id=user_id, session_id=session_id,
                include_deleted=True,
            )
            if record is None:
                raise ConversationNotFoundError("session_not_found")
            if record.status == "deleted":
                return
            active_id = record.active_run_id
            if active_id:
                # 与现有完成路径一致：先锁 execution、run，再更新 session。
                DurableRunService(self._session).request_stop(active_id)
            expected_active = AgentSession.active_run_id.is_(None)
            if active_id:
                expected_active = or_(expected_active, AgentSession.active_run_id == active_id)
            changed = self._session.execute(
                update(AgentSession).where(
                    AgentSession.session_key == record.session_key,
                    AgentSession.status != "deleted", expected_active,
                ).values(status="deleted")
            )
            if changed.rowcount:
                return
            # 删除前有新的任务抢先开始，回滚本轮停止请求，重新读取并停止正确任务。
            self._session.rollback()
        raise ConversationConflictError("session_changed_retry")

    def begin_turn(self, data: BeginTurn) -> TurnHandle:
        existing = self._repository.get_turn_by_request(
            tenant_id=data.tenant_id,
            request_id=data.request_id,
        )
        if existing is not None:
            return self._replay_turn(data, existing)

        session_record = self._repository.get_or_create_session(
            tenant_id=data.tenant_id,
            user_id=data.user_id,
            session_id=data.session_id,
        )
        session_record = self._repository.lock_session(
            session_key=session_record.session_key,
            tenant_id=data.tenant_id,
            user_id=data.user_id,
        )

        existing = self._repository.get_turn_by_request(
            tenant_id=data.tenant_id,
            request_id=data.request_id,
        )
        if existing is not None:
            return self._replay_turn(data, existing)
        if session_record.active_run_id is not None:
            raise SessionBusyError("session_busy")

        run_id = _stable_run_id(data.tenant_id, data.request_id)
        trace_id = f"trace_{uuid4().hex}"
        user_message = AgentMessage(
            message_id=f"msg_{uuid4().hex}",
            tenant_id=data.tenant_id,
            user_id=data.user_id,
            session_key=session_record.session_key,
            run_id=run_id,
            request_id=data.request_id,
            role="user",
            status="completed",
            content=data.user_content,
            sequence_no=session_record.next_sequence_no,
            metadata_json=data.metadata.model_dump(mode="json"),
            completed_at=datetime.now(UTC),
        )
        assistant_message = AgentMessage(
            message_id=f"msg_{uuid4().hex}",
            tenant_id=data.tenant_id,
            user_id=data.user_id,
            session_key=session_record.session_key,
            run_id=run_id,
            request_id=data.request_id,
            role="assistant",
            status="pending",
            content="",
            sequence_no=session_record.next_sequence_no + 1,
            metadata_json={},
        )
        run = AgentRun(
            run_id=run_id,
            session_id=data.session_id,
            session_key=session_record.session_key,
            tenant_id=data.tenant_id,
            user_id=data.user_id,
            trace_id=trace_id,
            status="running",
        )

        try:
            with self._session.begin_nested():
                claimed = self._session.execute(
                    update(AgentSession).where(
                        AgentSession.session_key == session_record.session_key,
                        AgentSession.status != "deleted",
                        AgentSession.active_run_id.is_(None),
                        AgentSession.next_sequence_no == user_message.sequence_no,
                    ).values(
                        active_run_id=run_id,
                        next_sequence_no=AgentSession.next_sequence_no + 2,
                    ).execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    raise ConversationConflictError("session_changed_retry")
                self._session.expire(session_record, ["active_run_id", "next_sequence_no"])
                self._session.add_all([run, user_message, assistant_message])
                self._session.flush()
        except IntegrityError:
            self._session.expire_all()
            existing = self._repository.get_turn_by_request(
                tenant_id=data.tenant_id,
                request_id=data.request_id,
            )
            if existing is None:
                raise ConversationConflictError("turn_create_conflict") from None
            return self._replay_turn(data, existing)

        return TurnHandle(
            session_key=session_record.session_key,
            session_id=data.session_id,
            run_id=run_id,
            request_id=data.request_id,
            trace_id=trace_id,
            user_message_id=user_message.message_id,
            assistant_message_id=assistant_message.message_id,
        )

    def mark_streaming(self, *, tenant_id: str, run_id: str) -> None:
        self._transition(
            tenant_id=tenant_id,
            run_id=run_id,
            message_states={"pending", "streaming", "interrupted"},
            message_target="streaming",
            run_states={"running", "interrupted"},
            run_target="running",
        )

    def complete_turn(
        self,
        *,
        tenant_id: str,
        run_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._transition(
            tenant_id=tenant_id,
            run_id=run_id,
            message_states={"pending", "streaming", "interrupted"},
            message_target="completed",
            run_states={"running", "interrupted"},
            run_target="completed",
            content=content,
            metadata=metadata,
            clear_active_run=True,
        )

    def interrupt_turn(self, *, tenant_id: str, run_id: str) -> None:
        self._transition(
            tenant_id=tenant_id,
            run_id=run_id,
            message_states={"pending", "streaming"},
            message_target="interrupted",
            run_states={"running"},
            run_target="interrupted",
        )

    def reject_turn(
        self,
        *,
        tenant_id: str,
        run_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._transition(
            tenant_id=tenant_id,
            run_id=run_id,
            message_states={"interrupted"},
            message_target="completed",
            run_states={"interrupted"},
            run_target="rejected",
            content=content,
            metadata=metadata,
            clear_active_run=True,
        )

    def fail_turn(
        self,
        *,
        tenant_id: str,
        run_id: str,
        error_code: str,
    ) -> None:
        if _SAFE_ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("unsafe_error_code")
        self._transition(
            tenant_id=tenant_id,
            run_id=run_id,
            message_states={"pending", "streaming", "interrupted"},
            message_target="failed",
            run_states={"running", "interrupted"},
            run_target="failed",
            metadata={"error_code": error_code},
            clear_active_run=True,
        )

    def resume_turn(self, *, tenant_id: str, run_id: str) -> TurnHandle:
        run = self._repository.get_run(tenant_id=tenant_id, run_id=run_id)
        if run is None:
            raise ConversationNotFoundError("run_not_found")
        if run.status != "interrupted":
            raise ConversationConflictError("run_state_conflict")

        assistant = self._repository.get_assistant_by_run(
            tenant_id=tenant_id,
            run_id=run_id,
        )
        if assistant is None:
            raise ConversationIntegrityError("assistant_message_missing")
        if assistant.status != "interrupted":
            raise ConversationConflictError("message_state_conflict")
        turn = self._repository.get_turn_by_request(
            tenant_id=tenant_id,
            request_id=assistant.request_id,
        )
        if turn is None or turn[1].message_id != assistant.message_id:
            raise ConversationIntegrityError("turn_inconsistent")
        session_record = self._repository.get_session_by_key(
            tenant_id=tenant_id,
            session_key=run.session_key,
        )
        if session_record is None:
            raise ConversationIntegrityError("session_missing")
        if session_record.status == "deleted":
            raise ConversationConflictError("session_deleted")
        if session_record.active_run_id != run_id:
            raise ConversationIntegrityError("active_run_inconsistent")
        return self._handle_from_records(
            run=run,
            session_record=session_record,
            turn=turn,
            replayed=False,
        )

    def _replay_turn(
        self,
        data: BeginTurn,
        turn: tuple[AgentMessage, AgentMessage],
    ) -> TurnHandle:
        user_message, _ = turn
        run = self._repository.get_run(
            tenant_id=data.tenant_id,
            run_id=user_message.run_id,
        )
        session_record = self._repository.get_session_by_key(
            tenant_id=data.tenant_id,
            session_key=user_message.session_key,
        )
        if run is None or session_record is None:
            raise ConversationIntegrityError("turn_owner_missing")
        if session_record.status == "deleted":
            raise ConversationConflictError("session_deleted")
        if (
            user_message.user_id != data.user_id
            or user_message.content != data.user_content
            or session_record.session_id != data.session_id
            or user_message.metadata_json
            != data.metadata.model_dump(mode="json")
        ):
            raise ConversationConflictError("request_conflict")
        return self._handle_from_records(
            run=run,
            session_record=session_record,
            turn=turn,
            replayed=True,
        )

    def _handle_from_records(
        self,
        *,
        run: AgentRun,
        session_record: AgentSession,
        turn: tuple[AgentMessage, AgentMessage],
        replayed: bool,
    ) -> TurnHandle:
        user_message, assistant_message = turn
        if (
            run.session_key != session_record.session_key
            or run.session_key != user_message.session_key
            or run.session_id != session_record.session_id
            or run.user_id != user_message.user_id
            or run.run_id != user_message.run_id
            or run.tenant_id != user_message.tenant_id
        ):
            raise ConversationIntegrityError("turn_owner_inconsistent")
        return TurnHandle(
            session_key=session_record.session_key,
            session_id=session_record.session_id,
            run_id=run.run_id,
            request_id=user_message.request_id,
            trace_id=run.trace_id,
            user_message_id=user_message.message_id,
            assistant_message_id=assistant_message.message_id,
            replayed=replayed,
        )

    def _transition(
        self,
        *,
        tenant_id: str,
        run_id: str,
        message_states: set[str],
        message_target: str,
        run_states: set[str],
        run_target: str,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
        clear_active_run: bool = False,
    ) -> None:
        from app.conversations.execution import check_execution_cancelled, current_execution

        check_execution_cancelled(self._session)
        if current_execution.get() is not None and message_target in {"completed", "failed"}:
            from app.conversations.durable import DurableRunService, TurnCancelled

            if DurableRunService(self._session).has_running_tools(run_id):
                self._session.rollback()
                raise TurnCancelled(run_id)
        message_values: dict[str, Any] = {"status": message_target}
        if content is not None:
            message_values["content"] = content
        if metadata is not None:
            message_values["metadata_json"] = metadata
        if message_target in {"completed", "failed"}:
            message_values["completed_at"] = datetime.now(UTC)

        with self._session.begin_nested():
            message_result = self._session.execute(
                update(AgentMessage)
                .where(
                    AgentMessage.tenant_id == tenant_id,
                    AgentMessage.run_id == run_id,
                    AgentMessage.role == "assistant",
                    AgentMessage.status.in_(message_states),
                )
                .values(**message_values)
                .execution_options(synchronize_session=False)
            )
            if message_result.rowcount != 1:
                raise ConversationConflictError("message_state_conflict")

            run_result = self._session.execute(
                update(AgentRun)
                .where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.run_id == run_id,
                    AgentRun.status.in_(run_states),
                )
                .values(status=run_target)
                .execution_options(synchronize_session=False)
            )
            if run_result.rowcount != 1:
                raise ConversationConflictError("run_state_conflict")

            if clear_active_run:
                session_result = self._session.execute(
                    update(AgentSession)
                    .where(
                        AgentSession.tenant_id == tenant_id,
                        AgentSession.active_run_id == run_id,
                    )
                    .values(active_run_id=None)
                    .execution_options(synchronize_session=False)
                )
                if session_result.rowcount != 1:
                    raise ConversationConflictError("session_state_conflict")
            self._session.flush()
        self._session.expire_all()


def _stable_run_id(tenant_id: str, request_id: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}\0{request_id}".encode()).hexdigest()
    return f"run_{digest[:60]}"
