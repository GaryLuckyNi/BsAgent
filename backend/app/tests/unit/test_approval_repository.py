from concurrent.futures import ThreadPoolExecutor
import json
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.approvals.repository import (
    ApprovalConflictError,
    ApprovalRecordCreate,
    ApprovalRepository,
)
from app.storage.models import ApprovalRecord, Base


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_local() as db:
        yield db


def _pending(*, approval_id: str = "approval_1", tenant_id: str = "tenant_1"):
    return ApprovalRecordCreate(
        approval_id=approval_id,
        run_id="run_1",
        session_id="session_1",
        session_key="session_key_1",
        tenant_id=tenant_id,
        request_user_id="user_1",
        approval_type="tool_call",
        tool_name="mcp.mock.write_order",
        tool_version="mcp-http-v1",
        arguments_hash="hash_1",
        payload={"arguments_summary": {"symbol": "600519.SH"}},
        checkpoint_thread_id="run_1",
    )


def test_approval_repository_creates_pending_record_idempotently(session):
    repository = ApprovalRepository(session)

    first = repository.create_pending(_pending())
    second = repository.create_pending(_pending())

    assert first.approval_id == "approval_1"
    assert second.approval_id == "approval_1"
    assert second.status == "pending"
    assert repository.list_pending(tenant_id="tenant_1") == [second]
    payload = json.loads(second.payload_json)
    assert payload["session_key"] == "session_key_1"
    assert payload["checkpoint_namespace"] == ""


def test_approval_repository_rejects_same_id_with_changed_hash(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending())

    with pytest.raises(ApprovalConflictError, match="payload does not match"):
        repository.create_pending(
            _pending().model_copy(update={"arguments_hash": "changed_hash"})
        )


def test_approval_repository_filters_pending_records_by_tenant(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending(approval_id="approval_1", tenant_id="tenant_1"))
    repository.create_pending(_pending(approval_id="approval_2", tenant_id="tenant_2"))

    records = repository.list_pending(tenant_id="tenant_1")

    assert [record.approval_id for record in records] == ["approval_1"]


def test_approval_repository_completed_decision_is_idempotent(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending())

    claim = repository.claim_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_1",
    )
    first = repository.complete_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        claim_token=claim.claim_token,
        claim_generation=claim.claim_generation,
    )
    second = repository.claim_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_1",
    )

    assert first.status == "approved"
    assert second.acquired is False
    assert second.record.status == "approved"
    assert second.record.decided_by == "approver_1"
    assert second.record.decided_at is not None


def test_approval_repository_rejects_conflicting_second_decision(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending())
    claim = repository.claim_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_1",
    )
    repository.complete_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        claim_token=claim.claim_token,
        claim_generation=claim.claim_generation,
    )

    with pytest.raises(ApprovalConflictError, match="already decided"):
        repository.claim_resume(
            approval_id="approval_1",
            tenant_id="tenant_1",
            decision="rejected",
            decided_by="approver_2",
        )


def test_approval_repository_hides_cross_tenant_record(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending())

    assert repository.get(approval_id="approval_1", tenant_id="tenant_2") is None


def test_approval_repository_allows_only_one_concurrent_resume_claim(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'approval-claim.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_local() as db:
        ApprovalRepository(db).create_pending(_pending())
        db.commit()
    barrier = Barrier(2)

    def attempt_claim(actor_id: str) -> str:
        with session_local() as db:
            barrier.wait()
            try:
                claim = ApprovalRepository(db).claim_resume(
                    approval_id="approval_1",
                    tenant_id="tenant_1",
                    decision="approved",
                    decided_by=actor_id,
                )
                db.commit()
                return "claimed" if claim.acquired else "terminal"
            except ApprovalConflictError as exc:
                db.rollback()
                return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(attempt_claim, ["approver_1", "approver_2"])
        )

    assert outcomes.count("claimed") == 1
    assert sum("in progress" in outcome for outcome in outcomes) == 1


def test_approval_repository_failed_resume_can_be_reclaimed_by_same_decision(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending())
    first = repository.claim_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_1",
    )
    original_decided_at = first.record.decided_at
    repository.fail_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        claim_token=first.claim_token,
        claim_generation=first.claim_generation,
        error="checkpoint missing",
    )

    failed = repository.get(approval_id="approval_1", tenant_id="tenant_1")
    failed_status = failed.status
    failed_decision = failed.decision
    failed_error = failed.last_resume_error
    retry = repository.claim_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_2",
    )
    completed = repository.complete_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        claim_token=retry.claim_token,
        claim_generation=retry.claim_generation,
    )

    assert failed_status == "resume_failed"
    assert failed_decision == "approved"
    assert failed_error == "checkpoint missing"
    assert retry.acquired is True
    assert retry.claim_token != first.claim_token
    assert retry.record.resume_attempts == 2
    assert completed.decided_by == "approver_1"
    assert completed.decided_at == original_decided_at
    assert completed.resume_claimed_by == "approver_2"
    assert completed.last_resume_error == "checkpoint missing"


def test_approval_repository_reclaims_expired_resume_lease(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending())
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    first = repository.claim_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_1",
        now=started_at,
        claim_timeout=timedelta(seconds=300),
    )

    reclaimed = repository.claim_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_2",
        now=started_at + timedelta(seconds=301),
        claim_timeout=timedelta(seconds=300),
    )

    assert reclaimed.acquired is True
    assert reclaimed.claim_token != first.claim_token
    assert reclaimed.record.resume_attempts == 2
    assert reclaimed.record.resume_claimed_by == "approver_2"
    assert reclaimed.claim_generation == first.claim_generation + 1


def test_stale_resume_generation_cannot_complete_or_fail_reclaimed_work(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending())
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    first = repository.claim_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_1",
        now=started_at,
        claim_timeout=timedelta(seconds=300),
    )
    reclaimed = repository.claim_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_2",
        now=started_at + timedelta(seconds=301),
        claim_timeout=timedelta(seconds=300),
    )

    with pytest.raises(ApprovalConflictError, match="no longer owned"):
        repository.complete_resume(
            approval_id="approval_1",
            tenant_id="tenant_1",
            claim_token=reclaimed.claim_token,
            claim_generation=first.claim_generation,
        )
    with pytest.raises(ApprovalConflictError, match="no longer owned"):
        repository.fail_resume(
            approval_id="approval_1",
            tenant_id="tenant_1",
            claim_token=reclaimed.claim_token,
            claim_generation=first.claim_generation,
            error="stale worker",
        )

    completed = repository.complete_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        claim_token=reclaimed.claim_token,
        claim_generation=reclaimed.claim_generation,
    )
    assert completed.status == "approved"


def test_approval_repository_does_not_steal_fresh_resume_lease(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending())
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    repository.claim_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_1",
        now=started_at,
        claim_timeout=timedelta(seconds=300),
    )

    with pytest.raises(ApprovalConflictError, match="in progress"):
        repository.claim_resume(
            approval_id="approval_1",
            tenant_id="tenant_1",
            decision="approved",
            decided_by="approver_2",
            now=started_at + timedelta(seconds=299),
            claim_timeout=timedelta(seconds=300),
        )


def test_approval_repository_does_not_reclaim_stale_lease_with_new_decision(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending())
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    repository.claim_resume(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_1",
        now=started_at,
        claim_timeout=timedelta(seconds=300),
    )

    with pytest.raises(ApprovalConflictError, match="already decided as approved"):
        repository.claim_resume(
            approval_id="approval_1",
            tenant_id="tenant_1",
            decision="rejected",
            decided_by="approver_2",
            now=started_at + timedelta(seconds=301),
            claim_timeout=timedelta(seconds=300),
        )


def test_approval_repository_concurrent_create_pending_is_idempotent(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'approval-create.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    insert_barrier = Barrier(2)

    @event.listens_for(engine, "before_cursor_execute")
    def synchronize_competing_inserts(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        if statement.lstrip().upper().startswith("INSERT INTO APPROVAL_RECORDS"):
            insert_barrier.wait()

    def create_same_approval(_: int) -> str:
        with session_local() as db:
            record = ApprovalRepository(db).create_pending(_pending())
            db.commit()
            return record.approval_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        approval_ids = list(executor.map(create_same_approval, [1, 2]))

    with session_local() as db:
        count = db.scalar(select(func.count()).select_from(ApprovalRecord))

    assert approval_ids == ["approval_1", "approval_1"]
    assert count == 1
