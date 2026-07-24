from fastapi.testclient import TestClient
import pytest
from langgraph.types import Interrupt
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import approvals, sessions
from app.graph.state import create_initial_state
from app.graph.runtime import AgentGraphExecutionError
from app.llm.schemas import LlmCompletionResult
from app.main import create_app
from app.schemas.approvals import ApprovalRequest
from app.schemas.memory import MemoryCandidate
from app.storage.database import get_db
from app.storage.models import (
    AgentRun,
    AgentTraceEvent,
    ApprovalRecord,
    Base,
    ExperienceEntryRecord,
)


class PolicyQaLlmClient:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_):
        self.calls += 1
        if self.calls == 1:
            return LlmCompletionResult(
                content="""
                {
                  "intent": "knowledge_qa",
                  "domain": "finance_policy",
                  "sub_intent": "policy_lookup",
                  "candidate_skill": "policy_qa",
                  "candidate_tool": null,
                  "risk_level": "medium",
                  "confidence": 0.84,
                  "needs_clarification": false,
                  "reason_codes": ["classifier_selected_policy_qa"]
                }
                """,
                model="fake-intent-model",
                provider="fake",
            )
        return LlmCompletionResult(
            content="差旅报销需要提交发票和审批单。",
            model="fake-render-model",
            provider="fake",
        )


@pytest.fixture(autouse=True)
def avoid_live_llm_provider(monkeypatch):
    monkeypatch.setattr(sessions, "maybe_build_llm_client", lambda: None)


def create_test_app_with_database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        with testing_session_local() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    return app, testing_session_local


def test_post_session_message_returns_agent_response(monkeypatch):
    monkeypatch.setattr(sessions.settings, "use_mock_rag", True)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "差旅报销需要什么？",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"].startswith("trace_")
    assert "差旅报销需要提交发票和审批单" in body["content"]
    assert body["approval_status"] == "not_required"


def test_post_session_message_persists_run_and_trace_events(monkeypatch):
    monkeypatch.setattr(sessions.settings, "use_mock_rag", True)
    app, testing_session_local = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "差旅报销需要什么？",
        },
    )

    assert response.status_code == 200
    with testing_session_local() as session:
        saved_run = session.scalars(select(AgentRun)).one()
        saved_events = session.scalars(select(AgentTraceEvent)).all()

    assert saved_run.session_id == "s1"
    assert saved_run.status == "completed"
    assert saved_run.trace_id == response.json()["trace_id"]
    assert {event.node_name for event in saved_events} >= {"intent_router", "final_response"}


def test_post_session_message_records_short_term_session_event(monkeypatch):
    captured = {}

    class RecordingShortTermStore:
        def append_session_event(self, *, tenant_id, session_id, event, ttl_seconds=None):
            captured["tenant_id"] = tenant_id
            captured["session_id"] = session_id
            captured["event"] = event
            captured["ttl_seconds"] = ttl_seconds

    def fake_graph(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["intent"] = {
            "intent": "knowledge_qa",
            "risk_level": "medium",
            "needs_clarification": False,
        }
        state["final_response"] = {
            "content": "ok",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    monkeypatch.setattr(
        sessions,
        "build_short_term_memory_store",
        lambda: RecordingShortTermStore(),
    )
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "差旅报销需要什么？",
        },
    )

    assert response.status_code == 200
    assert captured["tenant_id"] == "t1"
    assert captured["session_id"] == "s1"
    assert captured["event"]["intent"] == "knowledge_qa"
    assert captured["event"]["approval_status"] == "not_required"
    assert captured["event"]["trace_id"] == response.json()["trace_id"]


def test_post_session_message_ignores_short_term_memory_failure(monkeypatch):
    class FailingShortTermStore:
        def append_session_event(self, *, tenant_id, session_id, event, ttl_seconds=None):
            raise RuntimeError("redis unavailable")

    def fake_graph(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["final_response"] = {
            "content": "ok",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    monkeypatch.setattr(
        sessions,
        "build_short_term_memory_store",
        lambda: FailingShortTermStore(),
    )
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "差旅报销需要什么？",
        },
    )

    assert response.status_code == 200
    assert response.json()["content"] == "ok"


def test_post_session_message_uses_configured_rag_mode(monkeypatch):
    captured = {}

    def fake_graph(**kwargs):
        captured["mock_evidence"] = kwargs["mock_evidence"]
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["intent"] = {
            "intent": "knowledge_qa",
            "risk_level": "medium",
            "needs_clarification": False,
            "candidate_skill": "policy_qa",
            "reason_codes": [],
            "next_node": "skill_agent",
        }
        state["final_response"] = {
            "content": "ok",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions.settings, "use_mock_rag", False, raising=False)
    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "差旅报销需要什么？",
        },
    )

    assert response.status_code == 200
    assert captured["mock_evidence"] is False


def test_post_session_message_uses_graph_checkpointer(monkeypatch):
    captured = {}
    checkpointer = object()

    def fake_graph(**kwargs):
        captured["checkpointer"] = kwargs.get("checkpointer")
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["final_response"] = {
            "content": "ok",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "build_graph_checkpointer", lambda: checkpointer, raising=False)
    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "差旅报销需要什么？",
        },
    )

    assert response.status_code == 200
    assert captured["checkpointer"] is checkpointer


def test_post_session_message_persists_approved_experience_memory(monkeypatch):
    def fake_graph(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["memory_candidates"] = [
            MemoryCandidate(
                candidate_id="memcand_1",
                memory_type="user_preference",
                content="所有的文档用中文",
                confidence=0.9,
                approval_status="approved",
                sensitivity="low",
                source="user_explicit_preference",
            )
        ]
        state["approval_requests"] = [
            ApprovalRequest(
                approval_id="approval_1",
                run_id=state["request_id"],
                approval_type="memory_write",
                risk_level="low",
                payload={"candidate_id": "memcand_1"},
                status="approved",
            )
        ]
        state["final_response"] = {
            "content": "ok",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, testing_session_local = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "tenant_1",
            "user_id": "user_1",
            "message": "记住我的语言偏好，所有的文档用中文",
        },
    )

    assert response.status_code == 200
    with testing_session_local() as session:
        saved = session.scalars(select(ExperienceEntryRecord)).one()

    assert saved.source_candidate_id == "memcand_1"
    assert saved.content == "所有的文档用中文"


def test_post_session_message_recalls_experience_memory_for_response(monkeypatch):
    monkeypatch.setattr(sessions.settings, "use_mock_rag", True)
    monkeypatch.setattr(sessions, "maybe_build_llm_client", lambda: PolicyQaLlmClient())
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ExperienceEntryRecord(
                entry_id="exp_1",
                tenant_id="tenant_1",
                user_id="user_1",
                memory_type="user_preference",
                content="所有回答用中文",
                source_candidate_id="memcand_existing",
                sensitivity="low",
                source="user_explicit_preference",
            )
        )
        session.commit()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "tenant_1",
            "user_id": "user_1",
            "message": "What documents are required for reimbursement?",
        },
    )

    assert response.status_code == 200
    assert response.json()["content"].startswith("按你的中文偏好回答：")


def test_approval_endpoint_rejects_analyst_and_allows_mock_approver():
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="pending",
            )
        )
        session.commit()
    client = TestClient(app)

    denied = client.get(
        "/api/approvals/pending",
        params={"tenant_id": "tenant_1", "actor_id": "user_1"},
    )
    response = client.get(
        "/api/approvals/pending",
        params={"tenant_id": "tenant_1", "actor_id": "approver_1"},
    )

    assert denied.status_code == 403
    assert response.status_code == 200
    assert response.json()["items"][0]["approval_id"] == "approval_1"


def test_post_session_message_persists_pending_tool_approval(monkeypatch):
    def interrupted_graph(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
            permission_scopes=kwargs["permission_scopes"],
        )
        state["intent"] = {
            "intent": "tool_action",
            "risk_level": "high",
            "needs_clarification": False,
        }
        state["__interrupt__"] = [
            Interrupt(
                value={
                    "approval_id": "approval_1",
                    "approval_type": "tool_call",
                    "run_id": state["request_id"],
                    "session_id": state["session_id"],
                    "tenant_id": state["tenant_id"],
                    "request_user_id": state["user_id"],
                    "tool_name": "mcp.mock.trade.submit",
                    "tool_version": "1.0.0",
                    "arguments_hash": "hash_1",
                    "arguments": {"symbol": "600519"},
                    "purpose": "提交模拟交易",
                    "checkpoint_thread_id": state["request_id"],
                    "status": "pending",
                }
            )
        ]
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", interrupted_graph)
    app, testing_session_local = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "tenant_1",
            "user_id": "user_1",
            "message": "提交一笔模拟交易",
        },
    )

    assert response.status_code == 200
    assert response.json()["approval_status"] == "pending"
    assert response.json()["approval_ids"] == ["approval_1"]
    with testing_session_local() as session:
        approval = session.get(ApprovalRecord, "approval_1")
        run = session.scalars(select(AgentRun)).one()
    assert approval.arguments_hash == "hash_1"
    assert run.status == "interrupted"


def test_mock_approver_decision_resumes_checkpoint_with_request_user_scopes(monkeypatch):
    captured = {}

    def fake_resume(**kwargs):
        captured.update(kwargs)
        state = create_initial_state(
            session_id="session_1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="提交一笔模拟交易",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = "run_1"
        state["trace_id"] = "trace_1"
        state["final_response"] = {
            "content": "模拟交易已提交。",
            "trace_id": "trace_1",
            "cited_evidence_ids": [],
            "approval_status": "approved",
        }
        return state

    monkeypatch.setattr(approvals, "resume_question_answer_graph", fake_resume)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="pending",
            )
        )
        session.commit()
    client = TestClient(app)

    response = client.post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 200
    assert captured["checkpoint_thread_id"] == "run_1"
    assert captured["approval_decision"] == {
        "approval_id": "approval_1",
        "arguments_hash": "hash_1",
        "decision": "approved",
    }
    assert set(captured["permission_scopes"]) >= {"mcp:mock", "rag:retrieve"}
    assert response.json()["run_status"] == "completed"
    with testing_session_local() as session:
        approval = session.get(ApprovalRecord, "approval_1")
        run = session.get(AgentRun, "run_1")
    assert approval.status == "approved"
    assert approval.decided_by == "approver_1"
    assert run.status == "completed"


def test_repeated_same_approval_decision_does_not_resume_checkpoint_again(monkeypatch):
    resume_calls = []

    def unexpected_resume(**kwargs):
        resume_calls.append(kwargs)
        raise AssertionError("相同审批决定不应重复恢复 checkpoint")

    monkeypatch.setattr(approvals, "resume_question_answer_graph", unexpected_resume)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="approved",
                decided_by="approver_1",
            )
        )
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="completed",
            )
        )
        session.commit()
    client = TestClient(app)

    response = client.post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 200
    assert response.json()["approval"]["status"] == "approved"
    assert response.json()["run_status"] == "completed"
    assert response.json()["final_response"] is None
    assert resume_calls == []


@pytest.mark.parametrize(
    ("resume_error", "expected_code"),
    [
        (
            AgentGraphExecutionError(
                node_name="approval_gate",
                message="工具参数已变化",
                code="approval_payload_mismatch",
                http_status=409,
            ),
            "approval_payload_mismatch",
        ),
        (RuntimeError("checkpoint missing"), "approval_resume_failed"),
    ],
)
def test_approval_resume_failure_is_409_and_remains_retryable(
    monkeypatch,
    resume_error,
    expected_code,
):
    def failing_resume(**_):
        raise resume_error

    monkeypatch.setattr(approvals, "resume_question_answer_graph", failing_resume)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="pending",
            )
        )
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="interrupted",
            )
        )
        session.commit()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == expected_code
    with testing_session_local() as session:
        approval = session.get(ApprovalRecord, "approval_1")
        run = session.get(AgentRun, "run_1")
    assert approval.status == "resume_failed"
    assert approval.decision == "approved"
    assert approval.resume_attempts == 1
    assert approval.last_resume_error
    assert run.status == "interrupted"


def test_approval_resume_failed_can_retry_same_decision(monkeypatch):
    resume_calls = []

    def successful_resume(**kwargs):
        resume_calls.append(kwargs)
        state = create_initial_state(
            session_id="session_1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="提交一笔模拟交易",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = "run_1"
        state["trace_id"] = "trace_1"
        state["final_response"] = {
            "content": "模拟交易已提交。",
            "trace_id": "trace_1",
            "cited_evidence_ids": [],
            "approval_status": "approved",
        }
        return state

    monkeypatch.setattr(approvals, "resume_question_answer_graph", successful_resume)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="resume_failed",
                decision="approved",
                resume_attempts=1,
                last_resume_error="checkpoint missing",
                decided_by="approver_1",
            )
        )
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="interrupted",
            )
        )
        session.commit()
    client = TestClient(app)

    response = client.post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 200
    assert len(resume_calls) == 1
    with testing_session_local() as session:
        approval = session.get(ApprovalRecord, "approval_1")
    assert approval.status == "approved"
    assert approval.resume_attempts == 2
    assert approval.last_resume_error is None


def test_approval_resume_in_progress_request_does_not_call_resume(monkeypatch):
    resume_calls = []
    monkeypatch.setattr(
        approvals,
        "resume_question_answer_graph",
        lambda **kwargs: resume_calls.append(kwargs),
    )
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="resuming",
                decision="approved",
                resume_claim_token="owned-by-another-request",
                resume_attempts=1,
                decided_by="approver_1",
            )
        )
        session.commit()
    client = TestClient(app)

    response = client.post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approval_resume_in_progress"
    assert resume_calls == []


def test_approval_resume_finalization_failure_is_409_and_retryable(monkeypatch):
    def successful_resume(**kwargs):
        state = create_initial_state(
            session_id="session_1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="提交一笔模拟交易",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = "run_1"
        state["trace_id"] = "trace_1"
        state["final_response"] = {
            "content": "模拟交易已提交。",
            "trace_id": "trace_1",
            "cited_evidence_ids": [],
            "approval_status": "approved",
        }
        return state

    monkeypatch.setattr(approvals, "resume_question_answer_graph", successful_resume)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    monkeypatch.setattr(
        approvals,
        "persist_agent_run",
        lambda **_: (_ for _ in ()).throw(RuntimeError("run persistence failed")),
    )
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="pending",
            )
        )
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="interrupted",
            )
        )
        session.commit()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approval_resume_failed"
    with testing_session_local() as session:
        approval = session.get(ApprovalRecord, "approval_1")
        run = session.get(AgentRun, "run_1")
    assert approval.status == "resume_failed"
    assert "run persistence failed" in approval.last_resume_error
    assert run.status == "interrupted"


def test_post_session_message_returns_500_for_graph_execution_error(monkeypatch):
    def failing_graph(**_):
        raise AgentGraphExecutionError(node_name="retriever_agent", message="rag unavailable")

    monkeypatch.setattr(sessions, "run_question_answer_graph", failing_graph)
    client = TestClient(create_app())

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "差旅报销需要什么？",
        },
    )

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "agent_graph_execution_failed",
            "node": "retriever_agent",
            "message": "rag unavailable",
        }
    }


def test_post_session_message_uses_graph_error_http_status(monkeypatch):
    def failing_graph(**_):
        raise AgentGraphExecutionError(
            node_name="retriever_agent",
            message="No Chroma index found.",
            code="rag_index_unavailable",
            http_status=503,
        )

    monkeypatch.setattr(sessions, "run_question_answer_graph", failing_graph)
    client = TestClient(create_app())

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "差旅报销需要什么？",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "rag_index_unavailable"


def test_post_session_message_stream_returns_answer_deltas_and_final(monkeypatch):
    def fake_graph(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["intent"] = {
            "intent": "tool_action",
            "risk_level": "low",
            "needs_clarification": False,
        }
        state["final_response"] = {
            "content": "计算完成：总和是 6。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "1+2+3=?",
        },
    ) as response:
        body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert "event: status" in body
    assert "event: answer_delta" in body
    assert "计算完成：总和是 6。" in body
    assert "event: final" in body
    assert "event: done" in body


def test_post_session_message_stream_returns_a_share_research_answer(monkeypatch):
    def fake_run_and_persist_agent_message(*, session_id, request, db):
        state = create_initial_state(
            session_id=session_id,
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            user_message=request.message,
        )
        state["final_response"] = {
            "content": (
                "公司概览\n"
                "贵州茅台，行业：食品饮料。\n\n"
                "免责声明\n"
                "以上内容仅用于投研辅助分析，不构成投资建议、买卖建议或收益承诺。"
            ),
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(
        sessions,
        "_run_and_persist_agent_message",
        fake_run_and_persist_agent_message,
    )
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={
            "tenant_id": "tenant_1",
            "user_id": "user_1",
            "message": "分析一下贵州茅台",
        },
    ) as response:
        body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert "event: trace_events" in body
    assert "公司概览" in body
    assert "不构成投资建议" in body
