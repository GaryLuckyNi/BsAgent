from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
from threading import Barrier

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.approvals.repository import (
    ApprovalConflictError,
    ApprovalRecordCreate,
    ApprovalRepository,
    ApprovalResumeInProgressError,
)
from app.storage.models import Base
from app.tests.support.postgres_store import open_isolated_postgres_store


@pytest.fixture
def live_approval_sessions():
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("未配置真实 PostgreSQL 审批 claim 测试地址。")

    with open_isolated_postgres_store(postgres_url) as live_postgres_store:
        engine = create_engine(
            make_url(live_postgres_store.dsn).set(drivername="postgresql+psycopg")
        )
        Base.metadata.create_all(engine)
        try:
            yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
        finally:
            engine.dispose()


def _pending() -> ApprovalRecordCreate:
    return ApprovalRecordCreate(
        approval_id="approval_1",
        run_id="run_1",
        tenant_id="tenant_1",
        approval_type="tool_call",
        subject_name="mcp.mock.trade.submit",
        risk_level="high",
        action_digest="digest_1",
        checkpoint_thread_id="checkpoint-thread-1",
        checkpoint_namespace="",
        checkpoint_id="checkpoint_1",
        interrupt_id="interrupt_1",
        requested_by="user_1",
    )


def _create_pending(session_factory) -> None:
    with session_factory() as db:
        ApprovalRepository(db).create_pending(_pending())
        db.commit()


def test_live_postgres_claim_resume_has_one_owner_across_connections(
    live_approval_sessions,
):
    _create_pending(live_approval_sessions)
    barrier = Barrier(2)
    claimed_at = datetime(2026, 9, 1, tzinfo=UTC)

    def claim(actor_id: str) -> tuple[str, int | None]:
        with live_approval_sessions() as db:
            barrier.wait()
            try:
                result = ApprovalRepository(db).claim_resume(
                    approval_id="approval_1",
                    tenant_id="tenant_1",
                    decision="approved",
                    decided_by=actor_id,
                    now=claimed_at,
                )
                db.commit()
                return ("acquired", result.claim_generation)
            except ApprovalResumeInProgressError:
                db.rollback()
                return ("in_progress", None)
            except ApprovalConflictError:
                db.rollback()
                return ("conflict", None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, ("approver_1", "approver_2")))

    assert [outcome[0] for outcome in outcomes].count("acquired") == 1
    assert sum(outcome[0] in {"in_progress", "conflict"} for outcome in outcomes) == 1
    assert next(generation for status, generation in outcomes if status == "acquired") == 1


def test_live_postgres_reclaimed_lease_fences_stale_owner(
    live_approval_sessions,
):
    _create_pending(live_approval_sessions)
    started_at = datetime(2026, 9, 1, tzinfo=UTC)
    with live_approval_sessions() as db:
        first = ApprovalRepository(db).claim_resume(
            approval_id="approval_1",
            tenant_id="tenant_1",
            decision="approved",
            decided_by="approver_1",
            now=started_at,
            claim_timeout=timedelta(minutes=5),
        )
        db.commit()

    with live_approval_sessions() as db:
        reclaimed = ApprovalRepository(db).claim_resume(
            approval_id="approval_1",
            tenant_id="tenant_1",
            decision="approved",
            decided_by="approver_2",
            now=started_at + timedelta(minutes=5),
            claim_timeout=timedelta(minutes=5),
        )
        db.commit()

    assert reclaimed.claim_generation == first.claim_generation + 1
    assert reclaimed.claim_token != first.claim_token

    with live_approval_sessions() as db:
        repository = ApprovalRepository(db)
        with pytest.raises(ApprovalConflictError, match="no longer owned"):
            repository.renew_resume_claim(
                approval_id="approval_1",
                tenant_id="tenant_1",
                claim_token=first.claim_token,
                claim_generation=first.claim_generation,
                now=started_at + timedelta(minutes=5),
            )
        with pytest.raises(ApprovalConflictError, match="no longer owned"):
            repository.complete_resume(
                approval_id="approval_1",
                tenant_id="tenant_1",
                claim_token=first.claim_token,
                claim_generation=first.claim_generation,
            )
        db.rollback()

    with live_approval_sessions() as db:
        completed = ApprovalRepository(db).complete_resume(
            approval_id="approval_1",
            tenant_id="tenant_1",
            claim_token=reclaimed.claim_token,
            claim_generation=reclaimed.claim_generation,
        )
        assert completed.status == "approved"
        db.commit()


def test_live_postgres_completed_decision_is_idempotent_and_conflicts_on_change(
    live_approval_sessions,
):
    _create_pending(live_approval_sessions)
    with live_approval_sessions() as db:
        repository = ApprovalRepository(db)
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
        db.commit()

    with live_approval_sessions() as db:
        repository = ApprovalRepository(db)
        repeated = repository.claim_resume(
            approval_id="approval_1",
            tenant_id="tenant_1",
            decision="approved",
            decided_by="approver_2",
        )
        assert repeated.acquired is False
        assert repeated.record.status == "approved"
        with pytest.raises(ApprovalConflictError, match="already decided as approved"):
            repository.claim_resume(
                approval_id="approval_1",
                tenant_id="tenant_1",
                decision="rejected",
                decided_by="approver_2",
            )
