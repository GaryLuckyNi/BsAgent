import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.conversations.repository import (
    ConversationConflictError,
    ConversationNotFoundError,
    SessionBusyError,
)
from app.conversations.service import BeginTurn, ConversationService
from app.core.config import settings
from app.storage.models import AgentMessage, AgentRun, AgentSession, Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        yield db


def _begin(
    *,
    request_id: str = "request_1",
    tenant_id: str = "tenant_1",
    user_id: str = "user_1",
    session_id: str = "session_1",
    user_content: str = "hello",
    metadata: dict | None = None,
) -> BeginTurn:
    return BeginTurn(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        request_id=request_id,
        user_content=user_content,
        metadata=metadata
        or {"attachment_ids": ("asset-1",), "content_type": "text"},
    )


def test_begin_turn_creates_one_run_and_two_consecutive_messages(session):
    handle = ConversationService(session).begin_turn(_begin())

    runs = list(session.scalars(select(AgentRun)))
    messages = list(
        session.scalars(select(AgentMessage).order_by(AgentMessage.sequence_no))
    )
    record = session.get(AgentSession, handle.session_key)

    assert len(runs) == 1
    assert len(messages) == 2
    assert [message.role for message in messages] == ["user", "assistant"]
    assert [message.status for message in messages] == ["completed", "pending"]
    assert [message.sequence_no for message in messages] == [1, 2]
    assert messages[0].metadata_json == {
        "attachment_ids": ["asset-1"],
        "content_type": "text",
    }
    assert messages[1].content == ""
    assert runs[0].status == "running"
    assert runs[0].session_key == handle.session_key
    assert runs[0].trace_id == handle.trace_id
    assert record.next_sequence_no == 3
    assert record.active_run_id == handle.run_id
    assert handle.replayed is False


def test_begin_turn_replays_identical_request_without_duplicate_records(session):
    service = ConversationService(session)

    first = service.begin_turn(_begin())
    repeated = service.begin_turn(_begin())

    assert repeated.model_copy(update={"replayed": False}) == first
    assert repeated.replayed is True
    assert session.scalar(select(func.count()).select_from(AgentRun)) == 1
    assert session.scalar(select(func.count()).select_from(AgentMessage)) == 2


def test_begin_turn_rejects_replay_with_different_normalized_metadata(session):
    service = ConversationService(session)
    service.begin_turn(_begin(metadata={"attachment_ids": ["asset-1"]}))

    with pytest.raises(ConversationConflictError, match="request_conflict"):
        service.begin_turn(_begin(metadata={"attachment_ids": ["asset-2"]}))

    assert session.scalar(select(func.count()).select_from(AgentRun)) == 1
    assert session.scalar(select(func.count()).select_from(AgentMessage)) == 2


@pytest.mark.parametrize(
    "metadata",
    [
        {"source": "unit_test"},
        {"token": "secret"},
        {"authorization": "Bearer secret"},
        {"provider_credential": "secret"},
        {"content_type": "application/json"},
        {"attachment_ids": ["../unsafe"]},
        {"attachment_ids": ["a" * 65]},
        {
            "attachment_ids": [
                f"asset-{index}"
                for index in range(settings.max_message_attachments + 1)
            ]
        },
    ],
)
def test_begin_turn_metadata_rejects_unknown_or_unsafe_values(metadata):
    with pytest.raises(ValidationError):
        _begin(metadata=metadata)


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("user_content", "different"),
        ("session_id", "different_session"),
        ("user_id", "different_user"),
    ],
)
def test_begin_turn_rejects_changed_idempotency_payload(
    session,
    changed_field,
    changed_value,
):
    service = ConversationService(session)
    service.begin_turn(_begin())
    changed = _begin().model_copy(update={changed_field: changed_value})

    with pytest.raises(ConversationConflictError, match="request_conflict"):
        service.begin_turn(changed)

    assert session.scalar(select(func.count()).select_from(AgentRun)) == 1
    assert session.scalar(select(func.count()).select_from(AgentMessage)) == 2


def test_begin_turn_blocks_second_request_while_session_is_active(session):
    service = ConversationService(session)
    service.begin_turn(_begin(request_id="request_1"))

    with pytest.raises(SessionBusyError, match="session_busy"):
        service.begin_turn(_begin(request_id="request_2"))

    assert session.scalar(select(func.count()).select_from(AgentRun)) == 1
    assert session.scalar(select(func.count()).select_from(AgentMessage)) == 2


def test_completed_and_failed_turns_clear_active_run(session):
    service = ConversationService(session)
    completed = service.begin_turn(_begin(request_id="request_completed"))

    service.complete_turn(
        tenant_id="tenant_1",
        run_id=completed.run_id,
        content="answer",
    )
    completed_message = session.get(AgentMessage, completed.assistant_message_id)
    completed_run = session.get(AgentRun, completed.run_id)
    record = session.get(AgentSession, completed.session_key)

    assert completed_message.status == "completed"
    assert completed_message.content == "answer"
    assert completed_message.completed_at is not None
    assert completed_run.status == "completed"
    assert record.active_run_id is None

    failed = service.begin_turn(_begin(request_id="request_failed"))
    service.fail_turn(
        tenant_id="tenant_1",
        run_id=failed.run_id,
        error_code="provider_timeout",
    )
    failed_message = session.get(AgentMessage, failed.assistant_message_id)
    failed_run = session.get(AgentRun, failed.run_id)

    assert failed_message.status == "failed"
    assert failed_message.metadata_json == {"error_code": "provider_timeout"}
    assert failed_message.completed_at is not None
    assert failed_run.status == "failed"
    assert record.active_run_id is None


def test_interrupted_turn_keeps_active_run_and_resumes_original_handle(session):
    service = ConversationService(session)
    handle = service.begin_turn(_begin())
    service.mark_streaming(tenant_id="tenant_1", run_id=handle.run_id)

    service.interrupt_turn(tenant_id="tenant_1", run_id=handle.run_id)
    before_counts = (
        session.scalar(select(func.count()).select_from(AgentRun)),
        session.scalar(select(func.count()).select_from(AgentMessage)),
    )
    resumed = service.resume_turn(tenant_id="tenant_1", run_id=handle.run_id)
    assistant = session.get(AgentMessage, handle.assistant_message_id)
    run = session.get(AgentRun, handle.run_id)
    record = session.get(AgentSession, handle.session_key)

    assert assistant.status == "interrupted"
    assert run.status == "interrupted"
    assert record.active_run_id == handle.run_id
    assert resumed == handle
    assert (
        session.scalar(select(func.count()).select_from(AgentRun)),
        session.scalar(select(func.count()).select_from(AgentMessage)),
    ) == before_counts

    service.mark_streaming(tenant_id="tenant_1", run_id=handle.run_id)
    assert assistant.status == "streaming"
    assert run.status == "running"


def test_illegal_and_repeated_state_transitions_raise_conflict(session):
    service = ConversationService(session)
    handle = service.begin_turn(_begin())

    service.mark_streaming(tenant_id="tenant_1", run_id=handle.run_id)
    with pytest.raises(ConversationConflictError, match="message_state_conflict"):
        service.mark_streaming(tenant_id="tenant_1", run_id=handle.run_id)

    service.complete_turn(
        tenant_id="tenant_1",
        run_id=handle.run_id,
        content="answer",
    )
    for transition in (
        lambda: service.complete_turn(
            tenant_id="tenant_1",
            run_id=handle.run_id,
            content="changed",
        ),
        lambda: service.interrupt_turn(
            tenant_id="tenant_1",
            run_id=handle.run_id,
        ),
        lambda: service.fail_turn(
            tenant_id="tenant_1",
            run_id=handle.run_id,
            error_code="late_failure",
        ),
    ):
        with pytest.raises(
            ConversationConflictError,
            match="message_state_conflict",
        ):
            transition()


def test_resume_and_state_updates_are_tenant_scoped(session):
    service = ConversationService(session)
    handle = service.begin_turn(_begin())
    service.interrupt_turn(tenant_id="tenant_1", run_id=handle.run_id)

    with pytest.raises(ConversationConflictError):
        service.mark_streaming(tenant_id="tenant_2", run_id=handle.run_id)
    with pytest.raises(ConversationNotFoundError):
        service.resume_turn(tenant_id="tenant_2", run_id=handle.run_id)
