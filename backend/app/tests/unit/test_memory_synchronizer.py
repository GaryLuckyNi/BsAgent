from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.memory.synchronizer import MemoryStoreSynchronizer
from app.storage.models import Base, MemoryCandidateRecord


class RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str, dict]] = []

    def put(self, namespace, key, value) -> None:
        self.calls.append((namespace, key, value))


class FailingStore:
    def put(self, namespace, key, value) -> None:
        raise RuntimeError("postgresql://secret@example.invalid/db")


def _candidate(**overrides) -> MemoryCandidateRecord:
    values = {
        "candidate_id": "candidate_1",
        "tenant_id": "tenant_1",
        "user_id": "user_1",
        "run_id": "run_1",
        "memory_type": "user_preference",
        "memory_key": "language",
        "content": "所有回答使用中文",
        "confidence": 0.9,
        "approval_status": "not_required",
        "sensitivity": "low",
        "source": "user_explicit_preference",
    }
    values.update(overrides)
    return MemoryCandidateRecord(**values)


def test_sync_run_writes_committed_eligible_candidate_with_stable_key():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as session:
        session.add(_candidate())
        session.commit()

    store = RecordingStore()
    assert MemoryStoreSynchronizer(session_factory=factory, store=store).sync_run("run_1") == 1

    with Session(engine) as session:
        candidate = session.get(MemoryCandidateRecord, "candidate_1")
        assert candidate.store_sync_status == "synced"
        assert candidate.store_sync_attempts == 1
        assert candidate.store_sync_error is None
    assert store.calls == [
        (
            ("tenants", "tenant_1", "users", "user_1", "experiences"),
            "language",
            {
                "memory_type": "user_preference",
                "memory_key": "language",
                "content": "所有回答使用中文",
                "confidence": 0.9,
                "source": "user_explicit_preference",
            },
        )
    ]


def test_sync_run_does_not_write_pending_or_rejected_candidates():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as session:
        session.add_all(
            [
                _candidate(candidate_id="pending", approval_status="pending"),
                _candidate(candidate_id="rejected", approval_status="rejected"),
            ]
        )
        session.commit()

    store = RecordingStore()
    assert MemoryStoreSynchronizer(session_factory=factory, store=store).sync_run("run_1") == 0
    assert store.calls == []


def test_sync_run_records_sanitized_failure_and_retries_same_key():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as session:
        session.add(_candidate())
        session.commit()

    assert MemoryStoreSynchronizer(session_factory=factory, store=FailingStore()).sync_run("run_1") == 0
    with Session(engine) as session:
        candidate = session.get(MemoryCandidateRecord, "candidate_1")
        assert candidate.store_sync_status == "failed"
        assert candidate.store_sync_attempts == 1
        assert candidate.store_sync_error == "store_write_failed:RuntimeError"

    succeeding_store = RecordingStore()
    assert MemoryStoreSynchronizer(session_factory=factory, store=succeeding_store).sync_run("run_1") == 1
    with Session(engine) as session:
        candidate = session.get(MemoryCandidateRecord, "candidate_1")
        assert candidate.store_sync_status == "synced"
        assert candidate.store_sync_attempts == 2
    assert succeeding_store.calls[0][1] == "language"
