from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.conversations.repository import (
    ConversationIntegrityError,
    ConversationNotFoundError,
    ConversationRepository,
)
from app.storage.models import AgentMessage, AgentRun, AgentSession, Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        yield db


def _message(
    *,
    message_id: str,
    session_key: str,
    run_id: str,
    request_id: str,
    role: str,
    status: str,
    sequence_no: int,
    tenant_id: str = "tenant_1",
    user_id: str = "user_1",
    deleted_at: datetime | None = None,
) -> AgentMessage:
    return AgentMessage(
        message_id=message_id,
        tenant_id=tenant_id,
        user_id=user_id,
        session_key=session_key,
        run_id=run_id,
        request_id=request_id,
        role=role,
        status=status,
        content=f"content-{sequence_no}",
        sequence_no=sequence_no,
        metadata_json={},
        deleted_at=deleted_at,
    )


def test_same_external_session_id_isolated_by_tenant_and_user(session):
    repository = ConversationRepository(session)

    first = repository.get_or_create_session(
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="shared",
    )
    second = repository.get_or_create_session(
        tenant_id="tenant_2",
        user_id="user_1",
        session_id="shared",
    )
    third = repository.get_or_create_session(
        tenant_id="tenant_1",
        user_id="user_2",
        session_id="shared",
    )

    assert len({first.session_key, second.session_key, third.session_key}) == 3
    assert first.session_key.startswith("sess_")
    assert repository.get_owned_session(
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="shared",
    ) == first
    assert repository.get_owned_session(
        tenant_id="tenant_1",
        user_id="missing",
        session_id="shared",
    ) is None


def test_list_owned_sessions_returns_latest_sessions_with_identity_isolation(session):
    session.add_all(
        [
            AgentSession(
                session_key="session_key_old",
                session_id="session_old",
                tenant_id="tenant_1",
                user_id="user_1",
                status="active",
                updated_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
            AgentSession(
                session_key="session_key_new",
                session_id="session_new",
                tenant_id="tenant_1",
                user_id="user_1",
                status="active",
                updated_at=datetime(2026, 8, 2, tzinfo=UTC),
            ),
            AgentSession(
                session_key="session_key_other_user",
                session_id="session_other_user",
                tenant_id="tenant_1",
                user_id="user_2",
                status="active",
                updated_at=datetime(2026, 8, 3, tzinfo=UTC),
            ),
        ]
    )
    session.flush()

    records = ConversationRepository(session).list_owned_sessions(
        tenant_id="tenant_1",
        user_id="user_1",
        limit=1,
    )

    assert [record.session_id for record in records] == ["session_new"]


def test_lock_session_requires_full_ownership(session):
    repository = ConversationRepository(session)
    record = repository.get_or_create_session(
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
    )

    assert repository.lock_session(
        session_key=record.session_key,
        tenant_id="tenant_1",
        user_id="user_1",
    ) == record
    with pytest.raises(ConversationNotFoundError, match="session_not_found"):
        repository.lock_session(
            session_key=record.session_key,
            tenant_id="tenant_2",
            user_id="user_1",
        )


def test_get_turn_by_request_returns_complete_consistent_pair(session):
    repository = ConversationRepository(session)
    user = _message(
        message_id="message_user",
        session_key="session_key_1",
        run_id="run_1",
        request_id="request_1",
        role="user",
        status="completed",
        sequence_no=1,
    )
    assistant = _message(
        message_id="message_assistant",
        session_key="session_key_1",
        run_id="run_1",
        request_id="request_1",
        role="assistant",
        status="pending",
        sequence_no=2,
    )
    session.add_all([assistant, user])
    session.flush()

    turn = repository.get_turn_by_request(
        tenant_id="tenant_1",
        request_id="request_1",
    )

    assert turn == (user, assistant)
    assert repository.get_turn_by_request(
        tenant_id="tenant_2",
        request_id="request_1",
    ) is None


def test_get_turn_by_request_rejects_missing_or_inconsistent_pair(session):
    repository = ConversationRepository(session)
    session.add(
        _message(
            message_id="missing_assistant",
            session_key="session_key_1",
            run_id="run_1",
            request_id="request_missing",
            role="user",
            status="completed",
            sequence_no=1,
        )
    )
    session.add_all(
        [
            _message(
                message_id="mismatched_user",
                session_key="session_key_1",
                run_id="run_1",
                request_id="request_mismatch",
                role="user",
                status="completed",
                sequence_no=2,
            ),
            _message(
                message_id="mismatched_assistant",
                session_key="session_key_1",
                run_id="run_2",
                request_id="request_mismatch",
                role="assistant",
                status="pending",
                sequence_no=3,
            ),
        ]
    )
    session.flush()

    with pytest.raises(ConversationIntegrityError, match="turn_incomplete"):
        repository.get_turn_by_request(
            tenant_id="tenant_1",
            request_id="request_missing",
        )
    with pytest.raises(ConversationIntegrityError, match="turn_inconsistent"):
        repository.get_turn_by_request(
            tenant_id="tenant_1",
            request_id="request_mismatch",
        )


def test_list_context_messages_filters_and_returns_stable_recent_window(session):
    deleted_at = datetime.now(UTC)
    records = [
        _message(
            message_id="message_1",
            session_key="session_key_1",
            run_id="run_1",
            request_id="request_1",
            role="user",
            status="completed",
            sequence_no=1,
        ),
        _message(
            message_id="message_2",
            session_key="session_key_1",
            run_id="run_2",
            request_id="request_2",
            role="assistant",
            status="failed",
            sequence_no=2,
        ),
        _message(
            message_id="message_3",
            session_key="session_key_1",
            run_id="run_3",
            request_id="request_3",
            role="assistant",
            status="completed",
            sequence_no=3,
            deleted_at=deleted_at,
        ),
        _message(
            message_id="message_4",
            session_key="session_key_1",
            run_id="run_4",
            request_id="request_4",
            role="assistant",
            status="pending",
            sequence_no=4,
        ),
        _message(
            message_id="message_5",
            session_key="session_key_1",
            run_id="run_5",
            request_id="request_5",
            role="user",
            status="completed",
            sequence_no=5,
        ),
        _message(
            message_id="message_6",
            session_key="session_key_1",
            run_id="run_6",
            request_id="request_6",
            role="assistant",
            status="completed",
            sequence_no=6,
        ),
        _message(
            message_id="other_session",
            session_key="session_key_2",
            run_id="run_7",
            request_id="request_7",
            role="assistant",
            status="completed",
            sequence_no=7,
        ),
        _message(
            message_id="other_tenant",
            session_key="session_key_1",
            run_id="run_8",
            request_id="request_8",
            role="assistant",
            status="completed",
            sequence_no=8,
            tenant_id="tenant_2",
        ),
        _message(
            message_id="other_user",
            session_key="session_key_1",
            run_id="run_9",
            request_id="request_9",
            role="assistant",
            status="completed",
            sequence_no=9,
            user_id="user_2",
        ),
        _message(
            message_id="current_user",
            session_key="session_key_1",
            run_id="current_run",
            request_id="current_request",
            role="user",
            status="completed",
            sequence_no=10,
        ),
    ]
    session.add_all(records)
    session.flush()

    messages = ConversationRepository(session).list_context_messages(
        tenant_id="tenant_1",
        user_id="user_1",
        session_key="session_key_1",
        exclude_request_id="current_request",
        limit=2,
    )

    assert [message.sequence_no for message in messages] == [5, 6]


@pytest.mark.parametrize(
    ("tenant_id", "user_id"),
    [
        ("tenant_2", "user_1"),
        ("tenant_1", "user_2"),
    ],
)
def test_list_context_messages_requires_message_ownership(
    session,
    tenant_id,
    user_id,
):
    session.add(
        _message(
            message_id="message_1",
            session_key="shared_internal_key",
            run_id="run_1",
            request_id="request_1",
            role="user",
            status="completed",
            sequence_no=1,
        )
    )
    session.flush()

    messages = ConversationRepository(session).list_context_messages(
        tenant_id=tenant_id,
        user_id=user_id,
        session_key="shared_internal_key",
        exclude_request_id="current_request",
        limit=10,
    )

    assert messages == []


def test_list_message_page_is_session_scoped_and_returns_ascending_page(session):
    session.add_all(
        [
            _message(
                message_id=f"message_{sequence_no}",
                session_key="session_key_1",
                run_id=f"run_{sequence_no}",
                request_id=f"request_{sequence_no}",
                role="user" if sequence_no % 2 else "assistant",
                status="completed",
                sequence_no=sequence_no,
            )
            for sequence_no in range(1, 6)
        ]
        + [
            _message(
                message_id="other_session",
                session_key="session_key_2",
                run_id="other_run",
                request_id="other_request",
                role="user",
                status="completed",
                sequence_no=3,
            )
        ]
    )
    session.flush()

    page = ConversationRepository(session).list_message_page(
        tenant_id="tenant_1",
        user_id="user_1",
        session_key="session_key_1",
        limit=2,
        before_sequence=5,
    )

    assert [message.sequence_no for message in page] == [3, 4]
    assert all(message.session_key == "session_key_1" for message in page)


@pytest.mark.parametrize(
    ("tenant_id", "user_id"),
    [
        ("tenant_2", "user_1"),
        ("tenant_1", "user_2"),
    ],
)
def test_list_message_page_requires_message_ownership(
    session,
    tenant_id,
    user_id,
):
    session.add(
        _message(
            message_id="message_1",
            session_key="shared_internal_key",
            run_id="run_1",
            request_id="request_1",
            role="user",
            status="completed",
            sequence_no=1,
        )
    )
    session.flush()

    page = ConversationRepository(session).list_message_page(
        tenant_id=tenant_id,
        user_id=user_id,
        session_key="shared_internal_key",
        limit=10,
    )

    assert page == []


def test_repository_never_loads_run_by_bare_id_across_tenants(session):
    session.add(
        AgentRun(
            run_id="run_1",
            session_id="shared",
            session_key="session_key_1",
            tenant_id="tenant_1",
            user_id="user_1",
            trace_id="trace_1",
            status="running",
        )
    )
    session.flush()
    repository = ConversationRepository(session)

    assert repository.get_run(tenant_id="tenant_1", run_id="run_1") is not None
    assert repository.get_run(tenant_id="tenant_2", run_id="run_1") is None
