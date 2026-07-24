import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select, update
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


class ApprovalRecordCreate(BaseModel):
    approval_id: str
    run_id: str
    session_id: str
    tenant_id: str
    request_user_id: str
    approval_type: Literal["tool_call"] = "tool_call"
    tool_name: str
    tool_version: str | None = None
    arguments_hash: str
    payload: dict[str, Any] = Field(default_factory=dict)
    checkpoint_thread_id: str


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
            session_id=data.session_id,
            tenant_id=data.tenant_id,
            request_user_id=data.request_user_id,
            approval_type=data.approval_type,
            tool_name=data.tool_name,
            tool_version=data.tool_version,
            arguments_hash=data.arguments_hash,
            payload_json=json.dumps(
                data.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            checkpoint_thread_id=data.checkpoint_thread_id,
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
                session_id=payload["session_id"],
                tenant_id=payload["tenant_id"],
                request_user_id=payload["request_user_id"],
                approval_type=payload["approval_type"],
                tool_name=payload["tool_name"],
                tool_version=payload.get("tool_version"),
                arguments_hash=payload["arguments_hash"],
                payload=payload,
                checkpoint_thread_id=payload["checkpoint_thread_id"],
            )
        )

    def get(self, *, approval_id: str, tenant_id: str) -> ApprovalRecord | None:
        return self._session.scalar(
            select(ApprovalRecord).where(
                ApprovalRecord.approval_id == approval_id,
                ApprovalRecord.tenant_id == tenant_id,
            )
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
    ) -> ApprovalResumeClaim:
        claim_token = uuid4().hex
        claimed_at = datetime.now(UTC)
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
                ),
            )
            .values(
                status="resuming",
                decision=decision,
                resume_claim_token=claim_token,
                resume_attempts=ApprovalRecord.resume_attempts + 1,
                last_resume_error=None,
                resume_claimed_at=claimed_at,
                decided_by=decided_by,
                decided_at=claimed_at,
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
            return ApprovalResumeClaim(record=record, acquired=False, claim_token=None)
        raise ApprovalConflictError(
            f"Approval {approval_id} cannot be claimed from status {record.status}."
        )

    def complete_resume(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        claim_token: str,
    ) -> ApprovalRecord:
        result = self._session.execute(
            update(ApprovalRecord)
            .where(
                ApprovalRecord.approval_id == approval_id,
                ApprovalRecord.tenant_id == tenant_id,
                ApprovalRecord.status == "resuming",
                ApprovalRecord.resume_claim_token == claim_token,
            )
            .values(
                status=ApprovalRecord.decision,
                resume_claim_token=None,
                resume_claimed_at=None,
                last_resume_error=None,
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
        error: str,
    ) -> ApprovalRecord:
        result = self._session.execute(
            update(ApprovalRecord)
            .where(
                ApprovalRecord.approval_id == approval_id,
                ApprovalRecord.tenant_id == tenant_id,
                ApprovalRecord.status == "resuming",
                ApprovalRecord.resume_claim_token == claim_token,
            )
            .values(
                status="resume_failed",
                resume_claim_token=None,
                resume_claimed_at=None,
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

def _matches_create(record: ApprovalRecord, data: ApprovalRecordCreate) -> bool:
    return (
        record.run_id == data.run_id
        and record.session_id == data.session_id
        and record.tenant_id == data.tenant_id
        and record.request_user_id == data.request_user_id
        and record.approval_type == data.approval_type
        and record.tool_name == data.tool_name
        and record.tool_version == data.tool_version
        and record.arguments_hash == data.arguments_hash
        and record.checkpoint_thread_id == data.checkpoint_thread_id
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
