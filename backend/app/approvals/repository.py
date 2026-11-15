from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.storage.models import ApprovalRecord


class ApprovalConflictError(RuntimeError):
    pass


class ApprovalNotFoundError(RuntimeError):
    pass


class ApprovalResumeInProgressError(ApprovalConflictError):
    pass


@dataclass(frozen=True)
class ApprovalResumeClaim:
    record: ApprovalRecord
    acquired: bool
    claim_token: str | None
    claim_generation: int | None


class ApprovalRecordCreate(BaseModel):
    approval_id: str
    run_id: str
    tenant_id: str
    approval_type: Literal["tool_call", "memory_write"]
    subject_name: str
    risk_level: Literal["low", "medium", "high"]
    action_digest: str
    checkpoint_thread_id: str
    checkpoint_namespace: str
    checkpoint_id: str
    interrupt_id: str
    requested_by: str


class ApprovalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_pending(self, data: ApprovalRecordCreate) -> ApprovalRecord:
        existing = self._session.get(ApprovalRecord, data.approval_id)
        if existing is not None:
            return _validate_existing_create(existing, data)

        record = ApprovalRecord(
            approval_id=data.approval_id,
            run_id=data.run_id,
            tenant_id=data.tenant_id,
            approval_type=data.approval_type,
            subject_name=data.subject_name,
            risk_level=data.risk_level,
            action_digest=data.action_digest,
            checkpoint_thread_id=data.checkpoint_thread_id,
            checkpoint_namespace=data.checkpoint_namespace,
            checkpoint_id=data.checkpoint_id,
            interrupt_id=data.interrupt_id,
            requested_by=data.requested_by,
            status="pending",
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
            return record
        except IntegrityError:
            self._session.expire_all()
            existing = self._session.get(ApprovalRecord, data.approval_id)
            if existing is None:
                raise ApprovalConflictError(
                    f"Approval {data.approval_id} could not be created idempotently."
                )
            return _validate_existing_create(existing, data)

    def create_pending_from_interrupt(self, payload: dict[str, Any]) -> ApprovalRecord:
        return self.create_pending(
            ApprovalRecordCreate(
                approval_id=payload["approval_id"],
                run_id=payload["run_id"],
                tenant_id=payload["tenant_id"],
                approval_type=payload["approval_type"],
                subject_name=payload["subject_name"],
                risk_level=payload["risk_level"],
                action_digest=payload["action_digest"],
                checkpoint_thread_id=payload["checkpoint_thread_id"],
                checkpoint_namespace=payload["checkpoint_namespace"],
                checkpoint_id=payload["checkpoint_id"],
                interrupt_id=payload["interrupt_id"],
                requested_by=payload["requested_by"],
            )
        )

    def get(self, *, approval_id: str, tenant_id: str) -> ApprovalRecord | None:
        return self._session.scalar(
            select(ApprovalRecord).where(
                ApprovalRecord.approval_id == approval_id,
                ApprovalRecord.tenant_id == tenant_id,
            )
        )

    def get_by_interrupt_id(
        self,
        *,
        interrupt_id: str,
        tenant_id: str,
    ) -> ApprovalRecord | None:
        return self._session.scalar(
            select(ApprovalRecord)
            .where(
                ApprovalRecord.interrupt_id == interrupt_id,
                ApprovalRecord.tenant_id == tenant_id,
            )
            .order_by(ApprovalRecord.created_at.desc())
            .limit(1)
        )

    def list_pending(self, *, tenant_id: str) -> list[ApprovalRecord]:
        return list(
            self._session.scalars(
                select(ApprovalRecord)
                .where(
                    ApprovalRecord.tenant_id == tenant_id,
                    ApprovalRecord.status.in_(
                        ("pending", "resuming", "resume_failed")
                    ),
                )
                .order_by(ApprovalRecord.created_at, ApprovalRecord.approval_id)
            )
        )

    def claim_resume(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        decision: Literal["approved", "rejected"],
        decided_by: str,
        now: datetime | None = None,
        claim_timeout: timedelta = timedelta(seconds=300),
    ) -> ApprovalResumeClaim:
        claim_token = uuid4().hex
        claimed_at = now or datetime.now(UTC)
        stale_before = claimed_at - claim_timeout
        result = self._session.execute(
            update(ApprovalRecord)
            .where(
                ApprovalRecord.approval_id == approval_id,
                ApprovalRecord.tenant_id == tenant_id,
                or_(
                    and_(
                        ApprovalRecord.status == "pending",
                        ApprovalRecord.decision.is_(None),
                    ),
                    and_(
                        ApprovalRecord.status == "resume_failed",
                        ApprovalRecord.decision == decision,
                    ),
                    and_(
                        ApprovalRecord.status == "resuming",
                        ApprovalRecord.decision == decision,
                        ApprovalRecord.resume_claimed_at.is_not(None),
                        ApprovalRecord.resume_claimed_at <= stale_before,
                    ),
                ),
            )
            .values(
                status="resuming",
                decision=decision,
                resume_claim_token=claim_token,
                resume_claim_generation=ApprovalRecord.resume_claim_generation + 1,
                resume_attempts=ApprovalRecord.resume_attempts + 1,
                resume_claimed_by=decided_by,
                resume_claimed_at=claimed_at,
                decided_by=case(
                    (ApprovalRecord.status == "pending", decided_by),
                    else_=ApprovalRecord.decided_by,
                ),
                decided_at=case(
                    (ApprovalRecord.status == "pending", claimed_at),
                    else_=ApprovalRecord.decided_at,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        self._session.expire_all()
        record = self.get(approval_id=approval_id, tenant_id=tenant_id)
        if result.rowcount == 1 and record is not None:
            return ApprovalResumeClaim(
                record=record,
                acquired=True,
                claim_token=claim_token,
                claim_generation=record.resume_claim_generation,
            )
        if record is None:
            raise ApprovalNotFoundError(f"Approval not found: {approval_id}")
        if record.decision is not None and record.decision != decision:
            raise ApprovalConflictError(
                f"Approval {approval_id} was already decided as {record.decision}."
            )
        if record.status == "resuming":
            raise ApprovalResumeInProgressError(
                f"Approval {approval_id} resume is already in progress."
            )
        if record.status in {"approved", "rejected"}:
            return ApprovalResumeClaim(
                record=record,
                acquired=False,
                claim_token=None,
                claim_generation=None,
            )
        raise ApprovalConflictError(
            f"Approval {approval_id} cannot be claimed from status {record.status}."
        )

    def complete_resume(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        claim_token: str,
        claim_generation: int,
    ) -> ApprovalRecord:
        result = self._session.execute(
            update(ApprovalRecord)
            .where(
                ApprovalRecord.approval_id == approval_id,
                ApprovalRecord.tenant_id == tenant_id,
                ApprovalRecord.status == "resuming",
                ApprovalRecord.resume_claim_token == claim_token,
                ApprovalRecord.resume_claim_generation == claim_generation,
            )
            .values(
                status=ApprovalRecord.decision,
                resume_claim_token=None,
            )
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        self._session.expire_all()
        record = self.get(approval_id=approval_id, tenant_id=tenant_id)
        if result.rowcount != 1 or record is None:
            raise ApprovalConflictError(
                f"Approval {approval_id} resume claim is no longer owned."
            )
        return record

    def fail_resume(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        claim_token: str,
        claim_generation: int,
        error: str,
    ) -> ApprovalRecord:
        result = self._session.execute(
            update(ApprovalRecord)
            .where(
                ApprovalRecord.approval_id == approval_id,
                ApprovalRecord.tenant_id == tenant_id,
                ApprovalRecord.status == "resuming",
                ApprovalRecord.resume_claim_token == claim_token,
                ApprovalRecord.resume_claim_generation == claim_generation,
            )
            .values(
                status="resume_failed",
                resume_claim_token=None,
                last_resume_error=error[:4000],
            )
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        self._session.expire_all()
        record = self.get(approval_id=approval_id, tenant_id=tenant_id)
        if result.rowcount != 1 or record is None:
            raise ApprovalConflictError(
                f"Approval {approval_id} resume claim is no longer owned."
            )
        return record

    def renew_resume_claim(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        claim_token: str,
        claim_generation: int,
        now: datetime | None = None,
    ) -> None:
        result = self._session.execute(
            update(ApprovalRecord)
            .where(
                ApprovalRecord.approval_id == approval_id,
                ApprovalRecord.tenant_id == tenant_id,
                ApprovalRecord.status == "resuming",
                ApprovalRecord.resume_claim_token == claim_token,
                ApprovalRecord.resume_claim_generation == claim_generation,
            )
            .values(resume_claimed_at=now or datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        if result.rowcount != 1:
            raise ApprovalConflictError(
                f"Approval {approval_id} resume claim is no longer owned."
            )

def _matches_create(record: ApprovalRecord, data: ApprovalRecordCreate) -> bool:
    return (
        record.run_id == data.run_id
        and record.tenant_id == data.tenant_id
        and record.approval_type == data.approval_type
        and record.subject_name == data.subject_name
        and record.risk_level == data.risk_level
        and record.action_digest == data.action_digest
        and record.checkpoint_thread_id == data.checkpoint_thread_id
        and record.checkpoint_namespace == data.checkpoint_namespace
        and record.checkpoint_id == data.checkpoint_id
        and record.interrupt_id == data.interrupt_id
        and record.requested_by == data.requested_by
    )


def _validate_existing_create(
    record: ApprovalRecord,
    data: ApprovalRecordCreate,
) -> ApprovalRecord:
    if not _matches_create(record, data):
        raise ApprovalConflictError(
            f"Approval {data.approval_id} payload does not match existing record."
        )
    return record
