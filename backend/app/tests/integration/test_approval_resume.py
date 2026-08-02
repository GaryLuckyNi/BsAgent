import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from langgraph.types import Interrupt
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import approvals
from app.graph.state import build_checkpoint_thread_id, create_initial_state
from app.harness.tracing import TraceEvent
from app.main import create_app
from app.security.dependencies import get_current_principal
from app.security.principal import Principal
from app.storage.database import get_db
from app.storage.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    AgentTraceEvent,
    ApprovalRecord,
    Base,
)


def _test_app():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        with session_factory() as session:
            yield session

    async def override_principal():
        return Principal(
            tenant_id="tenant_1",
            user_id="approver_1",
            scopes=("approval:decide",),
        )

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_principal] = override_principal
    return app, session_factory


def _seed_interrupted_turn(
    db,
    *,
    approval_status="pending",
    decision=None,
    run_status="interrupted",
    assistant_status="interrupted",
    active_run_id="run_1",
):
    checkpoint_thread_id = build_checkpoint_thread_id(
        tenant_id="tenant_1",
        session_key="session_key_1",
        request_id="request_1",
    )
    payload = {
        "run_id": "run_1",
        "session_id": "session_1",
        "session_key": "session_key_1",
        "tenant_id": "tenant_1",
        "request_user_id": "user_1",
        "checkpoint_thread_id": checkpoint_thread_id,
        "checkpoint_namespace": "",
    }
    db.add_all(
        [
            AgentSession(
                session_key="session_key_1",
                session_id="session_1",
                tenant_id="tenant_1",
                user_id="user_1",
                status="active",
                active_run_id=active_run_id,
                next_sequence_no=3,
            ),
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status=run_status,
            ),
            AgentMessage(
                message_id="user_message_1",
                tenant_id="tenant_1",
                user_id="user_1",
                session_key="session_key_1",
                run_id="run_1",
                request_id="request_1",
                role="user",
                status="completed",
                content="提交一笔模拟交易",
                sequence_no=1,
                completed_at=datetime.now(UTC),
            ),
            AgentMessage(
                message_id="assistant_message_1",
                tenant_id="tenant_1",
                user_id="user_1",
                session_key="session_key_1",
                run_id="run_1",
                request_id="request_1",
                role="assistant",
                status=assistant_status,
                content="",
                sequence_no=2,
            ),
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json=json.dumps(payload),
                checkpoint_thread_id=checkpoint_thread_id,
                status=approval_status,
                decision=decision,
                decided_by="approver_1" if decision else None,
            ),
        ]
    )
    db.commit()
    return checkpoint_thread_id


def _resumed_state(*, final_content=None, interrupted=False):
    state = create_initial_state(
        session_id="session_1",
        session_key="session_key_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="提交一笔模拟交易",
        request_id="request_1",
        trace_id="trace_1",
        user_message_id="user_message_1",
        assistant_message_id="assistant_message_1",
    )
    if final_content is not None:
        state["final_response"] = {
            "content": final_content,
            "trace_id": "trace_1",
            "cited_evidence_ids": [],
            "approval_status": "approved",
        }
    if interrupted:
        state["__interrupt__"] = [
            Interrupt(
                value={
                    "approval_id": "approval_2",
                    "approval_type": "tool_call",
                    "run_id": "run_1",
                    "session_id": "session_1",
                    "session_key": "session_key_1",
                    "tenant_id": "tenant_1",
                    "request_user_id": "user_1",
                    "tool_name": "mcp.mock.trade.submit",
                    "tool_version": "1.0.0",
                    "arguments_hash": "hash_2",
                    "arguments": {"symbol": "000001"},
                    "purpose": "第二笔交易",
                    "checkpoint_thread_id": build_checkpoint_thread_id(
                        tenant_id="tenant_1",
                        session_key="session_key_1",
                        request_id="request_1",
                    ),
                    "checkpoint_namespace": "",
                    "status": "pending",
                }
            )
        ]
    return state


def test_approval_resume_completes_original_assistant_message(monkeypatch):
    monkeypatch.setattr(
        approvals,
        "resume_question_answer_graph",
        lambda **_kwargs: _resumed_state(final_content="模拟交易已提交。"),
    )
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, session_factory = _test_app()
    with session_factory() as db:
        checkpoint_thread_id = _seed_interrupted_turn(db)

    response = TestClient(app).post(
        "/api/approvals/approval_1/decision",
        json={"decision": "approved"},
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "run_1"
    assert response.json()["request_id"] == "request_1"
    assert response.json()["message_id"] == "assistant_message_1"
    with session_factory() as db:
        assistant = db.get(AgentMessage, "assistant_message_1")
        run = db.get(AgentRun, "run_1")
        session_record = db.get(AgentSession, "session_key_1")
        messages = list(db.scalars(select(AgentMessage)))
    assert checkpoint_thread_id.startswith("cpr1_")
    assert len(messages) == 2
    assert assistant.status == "completed"
    assert assistant.content == "模拟交易已提交。"
    assert run.status == "completed"
    assert session_record.active_run_id is None


def test_approval_resume_uses_run_scoped_buffer_and_flushes_after_commit(monkeypatch):
    observed_buffers = []

    def resumed_with_trace(**kwargs):
        buffer = kwargs["trace_buffer"]
        state = _resumed_state(final_content="模拟交易已提交。")
        buffer.record_event(
            TraceEvent(
                trace_id=state["trace_id"],
                node_name="tool_agent",
                status="success",
                latency_ms=2,
            )
        )
        observed_buffers.append(buffer)
        return state

    monkeypatch.setattr(approvals, "resume_question_answer_graph", resumed_with_trace)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, session_factory = _test_app()
    with session_factory() as db:
        _seed_interrupted_turn(db)

    response = TestClient(app).post(
        "/api/approvals/approval_1/decision",
        json={"decision": "approved"},
    )

    assert response.status_code == 200
    assert len(observed_buffers) == 1
    with session_factory() as db:
        run = db.get(AgentRun, "run_1")
        events = list(db.scalars(select(AgentTraceEvent)))
    assert run.status == "completed"
    assert run.trace_capture_status == "captured"
    assert [(event.trace_id, event.run_id, event.node_name) for event in events] == [
        ("trace_1", "run_1", "tool_agent")
    ]


def test_approval_resume_interrupted_again_reuses_original_message(monkeypatch):
    monkeypatch.setattr(
        approvals,
        "resume_question_answer_graph",
        lambda **_kwargs: _resumed_state(interrupted=True),
    )
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, session_factory = _test_app()
    with session_factory() as db:
        _seed_interrupted_turn(db)

    response = TestClient(app).post(
        "/api/approvals/approval_1/decision",
        json={"decision": "approved"},
    )

    assert response.status_code == 200
    assert response.json()["message_id"] == "assistant_message_1"
    with session_factory() as db:
        assistant = db.get(AgentMessage, "assistant_message_1")
        run = db.get(AgentRun, "run_1")
        session_record = db.get(AgentSession, "session_key_1")
        messages = list(db.scalars(select(AgentMessage)))
    assert len(messages) == 2
    assert assistant.status == "interrupted"
    assert run.status == "interrupted"
    assert session_record.active_run_id == "run_1"


def test_rejected_approval_completes_original_message_with_rejected_run(monkeypatch):
    state = _resumed_state(final_content="用户拒绝了该操作。")
    state["final_response"]["approval_status"] = "rejected"
    monkeypatch.setattr(
        approvals,
        "resume_question_answer_graph",
        lambda **_kwargs: state,
    )
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, session_factory = _test_app()
    with session_factory() as db:
        _seed_interrupted_turn(db)

    response = TestClient(app).post(
        "/api/approvals/approval_1/decision",
        json={"decision": "rejected"},
    )

    assert response.status_code == 200
    with session_factory() as db:
        assistant = db.get(AgentMessage, "assistant_message_1")
        run = db.get(AgentRun, "run_1")
        session_record = db.get(AgentSession, "session_key_1")
    assert assistant.status == "completed"
    assert assistant.content == "用户拒绝了该操作。"
    assert assistant.metadata_json["approval_status"] == "rejected"
    assert run.status == "rejected"
    assert session_record.active_run_id is None


def test_repeated_approval_decision_returns_original_run_and_message(monkeypatch):
    resume_calls = []
    monkeypatch.setattr(
        approvals,
        "resume_question_answer_graph",
        lambda **kwargs: resume_calls.append(kwargs),
    )
    app, session_factory = _test_app()
    with session_factory() as db:
        _seed_interrupted_turn(
            db,
            approval_status="approved",
            decision="approved",
            run_status="completed",
            assistant_status="completed",
            active_run_id=None,
        )
        assistant = db.get(AgentMessage, "assistant_message_1")
        assistant.content = "已完成"
        db.commit()

    response = TestClient(app).post(
        "/api/approvals/approval_1/decision",
        json={"decision": "approved"},
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "run_1"
    assert response.json()["request_id"] == "request_1"
    assert response.json()["message_id"] == "assistant_message_1"
    assert response.json()["final_response"]["content"] == "已完成"
    assert resume_calls == []
    with session_factory() as db:
        assert len(list(db.scalars(select(AgentMessage)))) == 2


def test_approval_resume_failure_keeps_original_message_retryable(monkeypatch):
    def fail_resume(**_kwargs):
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(approvals, "resume_question_answer_graph", fail_resume)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, session_factory = _test_app()
    with session_factory() as db:
        _seed_interrupted_turn(db)

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/approvals/approval_1/decision",
        json={"decision": "approved"},
    )

    assert response.status_code == 409
    with session_factory() as db:
        assistant = db.get(AgentMessage, "assistant_message_1")
        run = db.get(AgentRun, "run_1")
        session_record = db.get(AgentSession, "session_key_1")
    assert assistant.status == "interrupted"
    assert assistant.content == ""
    assert run.status == "interrupted"
    assert session_record.active_run_id == "run_1"


def test_approval_resume_rejects_inconsistent_active_run_before_graph(monkeypatch):
    resume_calls = []
    monkeypatch.setattr(
        approvals,
        "resume_question_answer_graph",
        lambda **kwargs: resume_calls.append(kwargs),
    )
    app, session_factory = _test_app()
    with session_factory() as db:
        _seed_interrupted_turn(db, active_run_id=None)

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/approvals/approval_1/decision",
        json={"decision": "approved"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approval_resume_identity_mismatch"
    assert resume_calls == []
