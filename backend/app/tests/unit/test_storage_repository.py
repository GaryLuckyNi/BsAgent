import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.graph.runtime import run_question_answer_graph
from app.graph.state import create_initial_state
from app.harness.tracing import InMemoryTraceStore
from app.schemas.approvals import ApprovalRequest
from app.schemas.memory import ExperienceCommitPayload
from app.schemas.memory import MemoryCandidate
from app.storage.models import AgentRun, AgentSession, Base, MemoryCandidateRecord
from app.storage.repositories import persist_agent_run
from app.tools.registry import ToolRegistry


class RecordingExperienceStore:
    def __init__(self) -> None:
        self.payloads: list[ExperienceCommitPayload] = []

    def save(self, payload: ExperienceCommitPayload) -> None:
        self.payloads.append(payload)


def test_persist_agent_run_stores_session_and_run():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    trace_store = InMemoryTraceStore()
    state = run_question_answer_graph(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="你好",
        trace_store=trace_store,
        tool_registry_factory=ToolRegistry,
    )

    with Session(engine) as session:
        run = persist_agent_run(
            session=session,
            state=state,
            status="completed",
        )

        saved_session = session.get(AgentSession, state["session_key"])
        saved_run = session.get(AgentRun, run.run_id)

    assert saved_session.tenant_id == "tenant_1"
    assert saved_run.trace_id == state["trace_id"]
    assert saved_run.intent == "conversation"
    assert saved_run.risk_level == "low"


def test_persist_agent_run_does_not_implicitly_build_an_experience_store(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    state = create_initial_state(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="记住所有回答用中文",
    )
    state["memory_candidates"] = [
        MemoryCandidate(
            candidate_id="memcand_1",
            memory_type="user_preference",
            memory_key="language",
            content="所有回答用中文",
            confidence=1.0,
            approval_status="not_required",
            sensitivity="low",
        )
    ]

    with Session(engine) as session:
        persist_agent_run(session=session, state=state, status="completed")

    with Session(engine) as session:
        assert session.query(MemoryCandidateRecord).count() == 1
        from app.storage.models import ExperienceEntryRecord

        assert session.query(ExperienceEntryRecord).count() == 0


def test_persist_agent_run_updates_existing_run_when_resumed():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    state = create_initial_state(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="执行高风险工具",
    )
    with Session(engine) as session:
        persist_agent_run(
            session=session,
            state=state,
            status="interrupted",
            run_id="stable_run_1",
        )
        persist_agent_run(
            session=session,
            state=state,
            status="completed",
            run_id="stable_run_1",
        )

        saved_runs = session.scalars(select(AgentRun)).all()

    assert len(saved_runs) == 1
    assert saved_runs[0].run_id == "stable_run_1"
    assert saved_runs[0].status == "completed"


def test_persist_agent_run_stores_and_merges_memory_candidates():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    first_state = create_initial_state(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="记住我的联系偏好：发邮件到 alice@example.com",
    )
    first_state["memory_candidates"] = [
        MemoryCandidate(
            candidate_id="memcand_1",
            memory_type="user_preference",
            content="发邮件到 alice@example.com",
            confidence=0.7,
            approval_status="not_required",
            sensitivity="low",
            source="user_explicit_preference",
        )
    ]
    second_state = create_initial_state(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="记住我的联系偏好：发邮件到 alice@example.com",
    )
    second_state["memory_candidates"] = [
        MemoryCandidate(
            candidate_id="memcand_1",
            memory_type="user_preference",
            content="发邮件到 alice@example.com",
            confidence=0.95,
            approval_status="pending",
            sensitivity="high",
            source="user_explicit_preference",
        )
    ]

    with Session(engine) as session:
        persist_agent_run(
            session=session,
            state=first_state,
            status="completed",
        )
        persist_agent_run(
            session=session,
            state=second_state,
            status="completed",
        )

        saved_candidates = session.scalars(select(MemoryCandidateRecord)).all()

    assert len(saved_candidates) == 1
    assert saved_candidates[0].confidence == 0.95
    assert saved_candidates[0].sensitivity == "high"
    assert saved_candidates[0].approval_status == "pending"


def test_persist_agent_run_uses_override_run_id_for_memory_candidate():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    state = create_initial_state(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="记住偏好",
    )
    state["memory_candidates"] = [
        MemoryCandidate(
            candidate_id="memcand_override",
            memory_type="user_preference",
            content="使用中文",
            confidence=0.9,
            approval_status="not_required",
            sensitivity="low",
            source="user_explicit_preference",
        )
    ]

    with Session(engine) as session:
        persist_agent_run(
            session=session,
            state=state,
            status="completed",
            run_id="stable_run_1",
        )
        candidate = session.get(MemoryCandidateRecord, "memcand_override")
        run = session.get(AgentRun, "stable_run_1")
        request_run = session.get(AgentRun, state["request_id"])

    assert candidate.run_id == run.run_id == "stable_run_1"
    assert candidate.tenant_id == "tenant_1"
    assert candidate.user_id == "user_1"
    assert request_run is None


def test_persist_agent_run_rejects_cross_tenant_existing_session_key():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    state = create_initial_state(
        session_id="session_1",
        session_key="shared_session_key",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="问题",
    )

    with Session(engine) as session:
        session.add(
            AgentSession(
                session_key="shared_session_key",
                session_id="session_1",
                tenant_id="tenant_2",
                user_id="user_1",
            )
        )
        session.commit()

        with pytest.raises(ValueError, match="agent_session_identity_conflict"):
            persist_agent_run(
                session=session,
                state=state,
                status="completed",
            )


def test_persist_agent_run_commits_approved_memory_to_injected_experience_store():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    state = create_initial_state(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="记住我的语言偏好，所有文档用中文",
    )
    state["memory_candidates"] = [
        MemoryCandidate(
            candidate_id="memcand_1",
            memory_type="user_preference",
            content="所有文档用中文",
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
    experience_store = RecordingExperienceStore()

    with Session(engine) as session:
        persist_agent_run(
            session=session,
            state=state,
            status="completed",
            experience_store=experience_store,
        )

    assert len(experience_store.payloads) == 1
    assert experience_store.payloads[0].candidate_id == "memcand_1"
