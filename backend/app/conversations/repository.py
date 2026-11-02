from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.storage.models import AgentMessage, AgentRun, AgentSession


class ConversationNotFoundError(RuntimeError):
    pass


class ConversationConflictError(RuntimeError):
    pass


class ConversationIntegrityError(RuntimeError):
    pass


class SessionBusyError(ConversationConflictError):
    pass


class ConversationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_owned_session(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        include_deleted: bool = False,
    ) -> AgentSession | None:
        return self._session.scalar(
            select(AgentSession).where(
                AgentSession.tenant_id == tenant_id,
                AgentSession.user_id == user_id,
                AgentSession.session_id == session_id,
                True if include_deleted else AgentSession.status != "deleted",
            )
        )

    def list_owned_sessions(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
        query: str = "",
    ) -> list[AgentSession]:
        matching_message = select(AgentMessage.message_id).where(
            AgentMessage.session_key == AgentSession.session_key,
            AgentMessage.deleted_at.is_(None),
            func.lower(AgentMessage.content).contains(query.strip().lower(), autoescape=True),
        ).exists()
        return list(
            self._session.scalars(
                select(AgentSession)
                .where(
                    AgentSession.tenant_id == tenant_id,
                    AgentSession.user_id == user_id,
                    AgentSession.status != "deleted",
                    matching_message if query.strip() else True,
                )
                .order_by(
                    AgentSession.updated_at.desc(),
                    AgentSession.session_key.desc(),
                )
                .limit(limit)
            )
        )

    def session_summaries(
        self, *, records: list[AgentSession],
    ) -> dict[str, tuple[str, str]]:
        if not records:
            return {}
        first_question = (
            select(func.substr(AgentMessage.content, 1, 160))
            .where(
                AgentMessage.session_key == AgentSession.session_key,
                AgentMessage.role == "user",
                AgentMessage.deleted_at.is_(None),
            )
            .order_by(AgentMessage.sequence_no)
            .limit(1)
            .correlate(AgentSession)
            .scalar_subquery()
        )
        latest_message = (
            select(func.substr(AgentMessage.content, 1, 320))
            .where(
                AgentMessage.session_key == AgentSession.session_key,
                AgentMessage.deleted_at.is_(None),
                func.length(func.trim(AgentMessage.content)) > 0,
            )
            .order_by(AgentMessage.sequence_no.desc())
            .limit(1)
            .correlate(AgentSession)
            .scalar_subquery()
        )
        # records 已由归属查询筛选；一次查询获取摘要，不逐会话读取完整消息。
        rows = self._session.execute(
            select(AgentSession.session_key, first_question, latest_message)
            .where(AgentSession.session_key.in_([record.session_key for record in records]))
        )
        return {key: (title or "", preview or "") for key, title, preview in rows}

    def search_owned_messages(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str,
        limit: int,
        offset: int,
    ) -> list[tuple[AgentMessage, AgentSession]]:
        query = query.strip()
        if not query:
            return []
        rows = self._session.execute(
            select(AgentMessage, AgentSession)
            .join(AgentSession, AgentSession.session_key == AgentMessage.session_key)
            .where(
                AgentSession.tenant_id == tenant_id,
                AgentSession.user_id == user_id,
                AgentSession.status != "deleted",
                AgentMessage.tenant_id == tenant_id,
                AgentMessage.user_id == user_id,
                AgentMessage.deleted_at.is_(None),
                AgentMessage.role.in_(("user", "assistant")),
                func.length(func.trim(AgentMessage.content)) > 0,
                func.lower(AgentMessage.content).contains(query.lower(), autoescape=True),
            )
            .order_by(AgentMessage.created_at.desc(), AgentMessage.message_id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [(message, session) for message, session in rows]

    def get_or_create_session(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> AgentSession:
        existing = self.get_owned_session(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            include_deleted=True,
        )
        if existing is not None:
            if existing.status == "deleted":
                raise ConversationConflictError("session_deleted")
            return existing

        record = AgentSession(
            session_key=f"sess_{uuid4().hex}",
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            status="active",
            active_run_id=None,
            next_sequence_no=1,
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
            return record
        except IntegrityError:
            self._session.expire_all()
            existing = self.get_owned_session(
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                include_deleted=True,
            )
            if existing is None:
                raise ConversationConflictError("session_create_conflict") from None
            if existing.status == "deleted":
                raise ConversationConflictError("session_deleted")
            return existing

    def lock_session(
        self,
        *,
        session_key: str,
        tenant_id: str,
        user_id: str,
    ) -> AgentSession:
        record = self._session.scalar(
            select(AgentSession)
            .where(
                AgentSession.session_key == session_key,
                AgentSession.tenant_id == tenant_id,
                AgentSession.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise ConversationNotFoundError("session_not_found")
        if record.status == "deleted":
            raise ConversationConflictError("session_deleted")
        return record

    def get_turn_by_request(
        self,
        *,
        tenant_id: str,
        request_id: str,
    ) -> tuple[AgentMessage, AgentMessage] | None:
        messages = list(
            self._session.scalars(
                select(AgentMessage).where(
                    AgentMessage.tenant_id == tenant_id,
                    AgentMessage.request_id == request_id,
                    AgentMessage.role.in_(("user", "assistant")),
                )
            )
        )
        if not messages:
            return None
        if len(messages) != 2:
            raise ConversationIntegrityError("turn_incomplete")

        by_role = {message.role: message for message in messages}
        if set(by_role) != {"user", "assistant"}:
            raise ConversationIntegrityError("turn_incomplete")
        user_message = by_role["user"]
        assistant_message = by_role["assistant"]
        if not self._is_consistent_turn(user_message, assistant_message):
            raise ConversationIntegrityError("turn_inconsistent")
        return user_message, assistant_message

    def get_run(self, *, tenant_id: str, run_id: str) -> AgentRun | None:
        return self._session.scalar(
            select(AgentRun).where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.run_id == run_id,
            )
        )

    def get_session_by_key(
        self,
        *,
        tenant_id: str,
        session_key: str,
    ) -> AgentSession | None:
        return self._session.scalar(
            select(AgentSession).where(
                AgentSession.tenant_id == tenant_id,
                AgentSession.session_key == session_key,
            )
        )

    def get_assistant_by_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> AgentMessage | None:
        return self._session.scalar(
            select(AgentMessage).where(
                AgentMessage.tenant_id == tenant_id,
                AgentMessage.run_id == run_id,
                AgentMessage.role == "assistant",
            )
        )

    def list_context_messages(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_key: str,
        exclude_request_id: str,
        limit: int,
    ) -> list[AgentMessage]:
        records = list(
            self._session.scalars(
                select(AgentMessage)
                .where(
                    AgentMessage.tenant_id == tenant_id,
                    AgentMessage.user_id == user_id,
                    AgentMessage.session_key == session_key,
                    AgentMessage.request_id != exclude_request_id,
                    AgentMessage.status == "completed",
                    AgentMessage.deleted_at.is_(None),
                    AgentMessage.role.in_(("user", "assistant")),
                )
                .order_by(AgentMessage.sequence_no.desc())
                .limit(limit)
            )
        )
        records.reverse()
        return records

    def list_context_messages_after(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_key: str,
        exclude_request_id: str,
        after_sequence_no: int = 0,
    ) -> list[AgentMessage]:
        return list(
            self._session.scalars(
                select(AgentMessage)
                .where(
                    AgentMessage.tenant_id == tenant_id,
                    AgentMessage.user_id == user_id,
                    AgentMessage.session_key == session_key,
                    AgentMessage.sequence_no > after_sequence_no,
                    AgentMessage.request_id != exclude_request_id,
                    AgentMessage.status == "completed",
                    AgentMessage.deleted_at.is_(None),
                    AgentMessage.role.in_(("user", "assistant")),
                )
                .order_by(AgentMessage.sequence_no.asc())
            )
        )

    def list_message_page(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_key: str,
        limit: int,
        before_sequence: int | None = None,
    ) -> list[AgentMessage]:
        statement = select(AgentMessage).where(
            AgentMessage.tenant_id == tenant_id,
            AgentMessage.user_id == user_id,
            AgentMessage.session_key == session_key,
            AgentMessage.deleted_at.is_(None),
            AgentMessage.role.in_(("user", "assistant")),
        )
        if before_sequence is not None:
            statement = statement.where(
                AgentMessage.sequence_no < before_sequence
            )
        records = list(
            self._session.scalars(
                statement.order_by(AgentMessage.sequence_no.desc()).limit(limit)
            )
        )
        records.reverse()
        return records

    @staticmethod
    def _is_consistent_turn(
        user_message: AgentMessage,
        assistant_message: AgentMessage,
    ) -> bool:
        return (
            user_message.tenant_id == assistant_message.tenant_id
            and user_message.user_id == assistant_message.user_id
            and user_message.session_key == assistant_message.session_key
            and user_message.run_id == assistant_message.run_id
            and user_message.request_id == assistant_message.request_id
            and assistant_message.sequence_no == user_message.sequence_no + 1
        )
