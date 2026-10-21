import json

import pytest
from app.conversations.service import BeginTurn, ConversationService
from app.storage.models import AgentMessage, AgentRun, AgentRunToolReceipt, AgentSession, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def cancelled(db):
    handle = ConversationService(db).begin_turn(
        BeginTurn(
            tenant_id="t",
            user_id="u",
            session_id="s",
            request_id="r1",
            user_content="上一轮研究问题",
        )
    )
    assistant = db.get(AgentMessage, handle.assistant_message_id)
    assistant.content = "已生成部分" * 3000
    assistant.status = "cancelled"
    db.get(AgentRun, handle.run_id).status = "cancelled"
    db.get(AgentSession, handle.session_key).active_run_id = None
    db.add(
        AgentRunToolReceipt(
            receipt_id="receipt1",
            run_id=handle.run_id,
            owner_token="owner",
            tool_name="write_ticket",
            risk_level="high",
            status="unknown",
            result_summary={"error": "timeout", "api_key": "do-not-copy"},
        )
    )
    db.commit()
    return handle


def test_handoff_keeps_partial_answer_and_unknown_effect_without_loading_all_history(db):
    from app.conversations.handoff import load_cancelled_turn_handoff

    handle = cancelled(db)
    messages = load_cancelled_turn_handoff(
        db, tenant_id="t", user_id="u", session_key=handle.session_key, exclude_request_id="r2"
    )
    assert len(messages) == 1
    message = messages[0]
    assert message.role == "assistant"
    assert "上一轮研究问题" in message.content
    assert "已生成部分" in message.content
    assert "unknown" in message.content
    assert "write_ticket" in message.content
    assert "不完整" in message.content
    assert "do-not-copy" not in message.content
    assert len(message.content) <= 4000


def test_handoff_is_scoped_to_owner_and_only_the_immediately_previous_turn(db):
    from app.conversations.handoff import load_cancelled_turn_handoff

    handle = cancelled(db)
    assert (
        load_cancelled_turn_handoff(
            db,
            tenant_id="other",
            user_id="u",
            session_key=handle.session_key,
            exclude_request_id="r2",
        )
        == ()
    )
    assert (
        load_cancelled_turn_handoff(
            db,
            tenant_id="t",
            user_id="other",
            session_key=handle.session_key,
            exclude_request_id="r2",
        )
        == ()
    )
    next_handle = ConversationService(db).begin_turn(
        BeginTurn(
            tenant_id="t",
            user_id="u",
            session_id="s",
            request_id="r2",
            user_content="下一轮",
        )
    )
    ConversationService(db).complete_turn(
        tenant_id="t", run_id=next_handle.run_id, content="已完成"
    )
    db.commit()
    assert (
        load_cancelled_turn_handoff(
            db, tenant_id="t", user_id="u", session_key=handle.session_key, exclude_request_id="r3"
        )
        == ()
    )


def test_execution_snapshot_includes_handoff_when_history_limit_is_zero(db):
    from app.api import sessions
    from app.security.principal import Principal

    cancelled(db)
    handle = ConversationService(db).begin_turn(
        BeginTurn(
            tenant_id="t",
            user_id="u",
            session_id="s",
            request_id="r2",
            user_content="下一轮",
        )
    )
    snapshot = sessions._load_execution_snapshot(
        db=db,
        principal=Principal(tenant_id="t", user_id="u", scopes=()),
        session_id="s",
        request=sessions.SessionMessageRequest(message="下一轮"),
        handle=handle,
        attachment_resolver=None,
    )
    assert sessions.CONVERSATION_HISTORY_LIMIT == 0
    assert len(snapshot.history) == 1
    assert "上一轮研究问题" in snapshot.history[0].content


def test_handoff_prioritizes_late_unknown_effects_and_reports_omissions(db):
    from app.conversations.handoff import load_cancelled_turn_handoff

    handle = cancelled(db)
    for number in range(30):
        db.add(
            AgentRunToolReceipt(
                receipt_id=f"known_{number:02}",
                run_id=handle.run_id,
                owner_token="owner",
                tool_name=f"read_{number}",
                risk_level="low",
                status="settled",
                result_summary={"content": "已确认结果" * 100},
            )
        )
    db.add(
        AgentRunToolReceipt(
            receipt_id="last_unknown",
            run_id=handle.run_id,
            owner_token="owner",
            tool_name="charge_payment",
            risk_level="high",
            status="unknown",
            result_summary={"error": "timeout"},
        )
    )
    db.commit()
    content = load_cancelled_turn_handoff(
        db, tenant_id="t", user_id="u", session_key=handle.session_key, exclude_request_id="r2"
    )[0].content
    payload = json.loads(content.split("\n", 1)[1])
    assert len(content) <= 4000
    assert payload["unknown_receipts_count"] == 2
    assert payload["omitted_receipts_count"] > 0
    assert payload["omitted_unknown_receipts_count"] == 0
    assert any(item["tool"] == "charge_payment" for item in payload["tool_receipts"])


def test_handoff_remains_valid_and_warns_when_unknown_receipts_exceed_budget(db):
    from app.conversations.handoff import load_cancelled_turn_handoff

    handle = cancelled(db)
    for number in range(50):
        db.add(
            AgentRunToolReceipt(
                receipt_id=f"unknown_{number:02}",
                run_id=handle.run_id,
                owner_token="owner",
                tool_name=f"write_{number}_" + "事务" * 80,
                risk_level="high",
                status="unknown",
                result_summary={"error": "timeout"},
            )
        )
    db.commit()
    content = load_cancelled_turn_handoff(
        db, tenant_id="t", user_id="u", session_key=handle.session_key, exclude_request_id="r2"
    )[0].content
    payload = json.loads(content.split("\n", 1)[1])
    assert len(content) <= 4000
    assert payload["unknown_receipts_count"] == 51
    assert payload["omitted_unknown_receipts_count"] > 0
    assert "不能" in content and "重试" in content
