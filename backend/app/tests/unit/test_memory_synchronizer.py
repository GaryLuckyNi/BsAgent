from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.memory.synchronizer import MemoryStoreSynchronizer, sync_committed_run
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
    assert store.calls == [(
        ("tenants", "tenant_1", "users", "user_1", "experiences"),
        "language",
        {"memory_type": "user_preference", "memory_key": "language",
         "content": "所有回答使用中文", "confidence": 0.9, "source": "user_explicit_preference"},
    )]



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


def test_reconcile_is_bounded_stably_ordered_and_recovers_stale_syncing():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    created_at = datetime(2026, 9, 1, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all(
            [
                _candidate(
                    candidate_id="candidate_b",
                    run_id="run_b",
                    memory_key="detail",
                    store_sync_status="failed",
                    created_at=created_at,
                ),
                _candidate(
                    candidate_id="candidate_a",
                    run_id="run_a",
                    memory_key="language",
                    store_sync_status="syncing",
                    created_at=created_at,
                ),
                _candidate(
                    candidate_id="candidate_c",
                    run_id="run_c",
                    memory_key="focus",
                    created_at=created_at,
                ),
            ]
        )
        session.commit()

    store = RecordingStore()
    synchronizer = MemoryStoreSynchronizer(session_factory=factory, store=store)

    assert synchronizer.reconcile(limit=2) == 2
    assert [call[1] for call in store.calls] == ["language", "detail"]
    with Session(engine) as session:
        assert session.get(MemoryCandidateRecord, "candidate_a").store_sync_status == "synced"
        assert session.get(MemoryCandidateRecord, "candidate_b").store_sync_status == "synced"
        assert session.get(MemoryCandidateRecord, "candidate_c").store_sync_status == "pending"


def test_reconcile_excludes_current_run_from_opportunistic_retry():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as session:
        session.add_all(
            [
                _candidate(store_sync_status="failed"),
                _candidate(
                    candidate_id="historical",
                    run_id="run_old",
                    memory_key="detail",
                    store_sync_status="failed",
                ),
            ]
        )
        session.commit()

    store = RecordingStore()
    synchronizer = MemoryStoreSynchronizer(session_factory=factory, store=store)

    assert synchronizer.reconcile(limit=1, exclude_run_id="run_1") == 1
    assert [call[1] for call in store.calls] == ["detail"]


def test_post_commit_sync_opportunistically_reconciles_historical_run():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add_all(
            [
                _candidate(),
                _candidate(
                    candidate_id="historical",
                    run_id="run_old",
                    tenant_id="tenant_2",
                    user_id="user_2",
                    memory_key="detail",
                    store_sync_status="failed",
                ),
            ]
        )
        session.commit()
        store = RecordingStore()

        assert sync_committed_run(db=session, store=store, run_id="run_1") == 1

    assert [(call[0], call[1]) for call in store.calls] == [
        (("tenants", "tenant_1", "users", "user_1", "experiences"), "language"),
        (("tenants", "tenant_2", "users", "user_2", "experiences"), "detail"),
    ]
