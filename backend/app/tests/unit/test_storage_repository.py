from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.graph.runtime import run_question_answer_graph
from app.graph.state import create_initial_state
from app.harness.tracing import InMemoryTraceStore
from app.schemas.approvals import ApprovalRequest
from app.schemas.memory import ExperienceCommitPayload
from app.schemas.memory import MemoryCandidate
from app.storage.models import AgentRun, AgentSession, AgentTraceEvent, Base, MemoryCandidateRecord
from app.storage.repositories import persist_agent_run


class RecordingExperienceStore:
    def __init__(self) -> None:
        self.payloads: list[ExperienceCommitPayload] = []

    def save(self, payload: ExperienceCommitPayload) -> None:
        self.payloads.append(payload)


def test_persist_agent_run_stores_session_run_and_trace_events():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    trace_store = InMemoryTraceStore()
    state = run_question_answer_graph(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        trace_store=trace_store,
    )

    with Session(engine) as session:
        run = persist_agent_run(
            session=session,
            state=state,
            trace_events=trace_store.events,
            status="completed",
        )

        saved_session = session.get(AgentSession, "session_1")
        saved_run = session.get(AgentRun, run.run_id)
        saved_events = session.scalars(select(AgentTraceEvent)).all()

    assert saved_session.tenant_id == "tenant_1"
    assert saved_run.trace_id == state["trace_id"]
    assert saved_run.intent == "knowledge_qa"
    assert saved_run.risk_level == "medium"
    assert {event.node_name for event in saved_events} >= {"intent_router", "final_response"}


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
            trace_events=[],
            status="completed",
        )
        persist_agent_run(
            session=session,
            state=second_state,
            trace_events=[],
            status="completed",
        )

        saved_candidates = session.scalars(select(MemoryCandidateRecord)).all()

    assert len(saved_candidates) == 1
    assert saved_candidates[0].confidence == 0.95
    assert saved_candidates[0].sensitivity == "high"
    assert saved_candidates[0].approval_status == "pending"


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
            trace_events=[],
            status="completed",
            experience_store=experience_store,
        )

    assert len(experience_store.payloads) == 1
    assert experience_store.payloads[0].candidate_id == "memcand_1"
