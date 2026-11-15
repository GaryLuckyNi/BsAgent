import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.conversations.recovery import recover_interrupted_local_runs
from app.conversations.repository import SessionBusyError
from app.conversations.service import BeginTurn, ConversationService
from app.storage.models import AgentMessage, AgentRun, AgentSession, Base


def _turn(request_id: str, session_id: str) -> BeginTurn:
    return BeginTurn(
        tenant_id="tenant_1",
        user_id="user_1",
        session_id=session_id,
        request_id=request_id,
        user_content="分析下周 A 股",
    )


@pytest.mark.parametrize("was_streaming", [False, True])
def test_recovery_fails_orphaned_running_turn_and_unlocks_session(was_streaming):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        service = ConversationService(db)
        handle = service.begin_turn(_turn("request_1", "session_1"))
        if was_streaming:
            service.mark_streaming(tenant_id="tenant_1", run_id=handle.run_id)
        db.commit()

    with Session(engine) as db:
        with pytest.raises(SessionBusyError):
            ConversationService(db).begin_turn(_turn("request_2", "session_1"))
        db.rollback()

    assert recover_interrupted_local_runs(engine) == 1
    assert recover_interrupted_local_runs(engine) == 0

    with Session(engine) as db:
        assert db.get(AgentRun, handle.run_id).status == "failed"
        assistant = db.get(AgentMessage, handle.assistant_message_id)
        assert assistant.status == "failed"
        assert assistant.metadata_json == {"error_code": "agent_server_restarted"}
        assert db.get(AgentSession, handle.session_key).active_run_id is None
        assert ConversationService(db).begin_turn(_turn("request_2", "session_1"))


def test_recovery_preserves_interrupted_approval_turn():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        service = ConversationService(db)
        handle = service.begin_turn(_turn("request_approval", "session_approval"))
        service.interrupt_turn(tenant_id="tenant_1", run_id=handle.run_id)
        db.commit()

    assert recover_interrupted_local_runs(engine) == 0

    with Session(engine) as db:
        assert db.get(AgentRun, handle.run_id).status == "interrupted"
        assert db.get(AgentMessage, handle.assistant_message_id).status == "interrupted"
        assert db.get(AgentSession, handle.session_key).active_run_id == handle.run_id
