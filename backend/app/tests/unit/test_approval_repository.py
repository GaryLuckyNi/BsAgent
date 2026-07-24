import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.approvals.repository import (
    ApprovalConflictError,
    ApprovalRecordCreate,
    ApprovalRepository,
)
from app.storage.models import Base


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
        tenant_id=tenant_id,
        request_user_id="user_1",
        approval_type="tool_call",
        tool_name="mcp.mock.write_order",
        tool_version="mcp-stdio-v1",
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


def test_approval_repository_decides_pending_record_and_is_idempotent(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending())

    first = repository.decide(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_1",
    )
    second = repository.decide(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_1",
    )

    assert first.status == "approved"
    assert second.status == "approved"
    assert second.decided_by == "approver_1"
    assert second.decided_at is not None


def test_approval_repository_rejects_conflicting_second_decision(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending())
    repository.decide(
        approval_id="approval_1",
        tenant_id="tenant_1",
        decision="approved",
        decided_by="approver_1",
    )

    with pytest.raises(ApprovalConflictError, match="already decided"):
        repository.decide(
            approval_id="approval_1",
            tenant_id="tenant_1",
            decision="rejected",
            decided_by="approver_2",
        )


def test_approval_repository_hides_cross_tenant_record(session):
    repository = ApprovalRepository(session)
    repository.create_pending(_pending())

    assert repository.get(approval_id="approval_1", tenant_id="tenant_2") is None
