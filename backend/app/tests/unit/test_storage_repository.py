from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.memory.store import SqlAlchemyMemoryStore
from app.schemas.memory import MemoryCandidate
from app.storage.models import Base


def _candidate(**updates) -> MemoryCandidate:
    values = {
        "candidate_id": "memcand_1",
        "memory_type": "user_preference",
        "memory_key": "language",
        "content": "使用中文",
        "confidence": 0.9,
        "approval_status": "not_required",
        "sensitivity": "low",
    }
    values.update(updates)
    return MemoryCandidate(**values)


def test_memory_candidate_defaults_store_key_and_pending_sync_state():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        record = SqlAlchemyMemoryStore(session).save_candidate(
            _candidate(memory_key=None),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )

    assert record.memory_key == "memcand_1"
    assert record.store_sync_status == "pending"
    assert record.store_sync_attempts == 0
    assert record.store_synced_at is None
    assert record.store_sync_error is None


def test_memory_candidate_resets_store_sync_only_for_effective_changes():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        store = SqlAlchemyMemoryStore(session)
        record = store.save_candidate(
            _candidate(),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )
        record.store_sync_status = "synced"
        record.store_synced_at = datetime.now(UTC)
        record.store_sync_error = "old error"
        session.flush()

        unchanged = store.save_candidate(
            _candidate(),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )
        assert unchanged.store_sync_status == "synced"
        assert unchanged.store_synced_at is not None
        assert unchanged.store_sync_error == "old error"

        changed = store.save_candidate(
            _candidate(content="使用简体中文"),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )

    assert changed.store_sync_status == "pending"
    assert changed.store_synced_at is None
    assert changed.store_sync_error is None


def test_commit_candidate_keeps_memory_key_in_compatibility_payload():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        store = SqlAlchemyMemoryStore(session)
        store.save_candidate(
            _candidate(),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )
        payload = store.commit_candidate("memcand_1")

    assert payload.memory_key == "language"
    assert payload.candidate_id == "memcand_1"
