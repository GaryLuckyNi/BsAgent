from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.memory.store import SqlAlchemyMemoryStore
from app.schemas.memory import MemoryCandidate
from app.storage.models import Base, MemoryCandidateRecord


def test_memory_store_persists_candidate_separately_from_entry():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    candidate = MemoryCandidate(
        candidate_id="memcand_1",
        memory_type="user_preference",
        content="所有的文档用中文",
        confidence=0.9,
        approval_status="not_required",
        sensitivity="low",
        source="user_explicit_preference",
    )

    with Session(engine) as session:
        store = SqlAlchemyMemoryStore(session)
        store.save_candidate(
            candidate,
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )
        session.commit()

        saved_candidate = session.get(MemoryCandidateRecord, "memcand_1")
    assert saved_candidate.content == "所有的文档用中文"
    assert saved_candidate.confidence == 0.9
    assert saved_candidate.approval_status == "not_required"
    assert "memory_entries" not in Base.metadata.tables


def test_memory_store_commit_candidate_returns_experience_payload_and_marks_candidate_approved():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    candidate = MemoryCandidate(
        candidate_id="memcand_1",
        memory_type="user_preference",
        content="所有的文档用中文",
        confidence=0.9,
        approval_status="not_required",
        sensitivity="low",
        source="user_explicit_preference",
    )

    with Session(engine) as session:
        store = SqlAlchemyMemoryStore(session)
        store.save_candidate(
            candidate,
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )
        payload = store.commit_candidate("memcand_1")
        session.commit()

        saved_candidate = session.get(MemoryCandidateRecord, "memcand_1")

    assert saved_candidate.approval_status == "approved"
    assert payload.candidate_id == "memcand_1"
    assert payload.tenant_id == "tenant_1"
    assert payload.user_id == "user_1"
    assert payload.memory_type == "user_preference"
    assert payload.content == "所有的文档用中文"


def test_memory_store_merges_duplicate_candidate_by_id():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    low_risk_candidate = MemoryCandidate(
        candidate_id="memcand_1",
        memory_type="user_preference",
        content="发邮件到 alice@example.com",
        confidence=0.7,
        approval_status="not_required",
        sensitivity="low",
        source="user_explicit_preference",
    )
    sensitive_duplicate = MemoryCandidate(
        candidate_id="memcand_1",
        memory_type="user_preference",
        content="发邮件到 alice@example.com",
        confidence=0.95,
        approval_status="pending",
        sensitivity="high",
        source="user_explicit_preference",
    )

    with Session(engine) as session:
        store = SqlAlchemyMemoryStore(session)
        store.save_candidate(
            low_risk_candidate,
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )
        store.save_candidate(
            sensitive_duplicate,
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_2",
        )
        session.commit()

        count = session.query(MemoryCandidateRecord).count()
        saved_candidate = session.get(MemoryCandidateRecord, "memcand_1")

    assert count == 1
    assert saved_candidate.confidence == 0.95
    assert saved_candidate.sensitivity == "high"
    assert saved_candidate.approval_status == "pending"
