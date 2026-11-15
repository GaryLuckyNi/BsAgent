from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.schemas.memory import MemoryCandidate
from app.storage.memory_candidates import MemoryCandidateRepository
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
        record = MemoryCandidateRepository(session).save_candidate(
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
        repository = MemoryCandidateRepository(session)
        record = repository.save_candidate(
            _candidate(),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )
        record.store_sync_status = "synced"
        record.store_synced_at = datetime.now(UTC)
        record.store_sync_error = "old error"
        session.flush()

        unchanged = repository.save_candidate(
            _candidate(),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )
        assert unchanged.store_sync_status == "synced"
        assert unchanged.store_synced_at is not None
        assert unchanged.store_sync_error == "old error"

        changed = repository.save_candidate(
            _candidate(content="使用简体中文"),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )

    assert changed.store_sync_status == "pending"
    assert changed.store_synced_at is None
    assert changed.store_sync_error is None


def test_memory_candidate_confidence_merge_keeps_synced_state():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        repository = MemoryCandidateRepository(session)
        record = repository.save_candidate(
            _candidate(),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )
        synced_at = datetime.now(UTC)
        record.store_sync_status = "synced"
        record.store_synced_at = synced_at
        record.store_sync_error = "old error"
        session.flush()

        merged = repository.save_candidate(
            _candidate(confidence=0.95),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )

    assert merged.confidence == 0.95
    assert merged.store_sync_status == "synced"
    # 仓储刷新后SQLite返回无时区UTC；仍核对同一时间，不依赖ORM旧缓存。
    assert merged.store_synced_at.replace(tzinfo=UTC) == synced_at
    assert merged.store_sync_error == "old error"


@pytest.mark.parametrize(
    "updates",
    (
        {"sensitivity": "high"},
        {"source": "user_explicit_preference"},
    ),
)
def test_memory_candidate_store_value_change_resets_synced_state(updates):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        repository = MemoryCandidateRepository(session)
        record = repository.save_candidate(
            _candidate(),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )
        record.store_sync_status = "synced"
        record.store_synced_at = datetime.now(UTC)
        record.store_sync_error = "old error"
        session.flush()

        changed = repository.save_candidate(
            _candidate(**updates),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )

    assert changed.store_sync_status == "pending"
    assert changed.store_synced_at is None
    assert changed.store_sync_error is None


def test_memory_candidate_lower_sensitivity_keeps_synced_state():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        repository = MemoryCandidateRepository(session)
        record = repository.save_candidate(
            _candidate(sensitivity="high"),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )
        synced_at = datetime.now(UTC)
        record.store_sync_status = "synced"
        record.store_synced_at = synced_at
        session.flush()

        merged = repository.save_candidate(
            _candidate(sensitivity="low"),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="run_1",
        )

    assert merged.sensitivity == "high"
    assert merged.store_sync_status == "synced"
