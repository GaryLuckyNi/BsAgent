import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.storage.models import ApprovalRecord


class ApprovalConflictError(RuntimeError):
    pass


class ApprovalNotFoundError(RuntimeError):
    pass


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
            if not _matches_create(existing, data):
                raise ApprovalConflictError(
                    f"Approval {data.approval_id} payload does not match existing record."
                )
            return existing

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
        self._session.add(record)
        self._session.flush()
        return record

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
                    ApprovalRecord.status == "pending",
                )
                .order_by(ApprovalRecord.created_at, ApprovalRecord.approval_id)
            )
        )

    def decide(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        decision: Literal["approved", "rejected"],
        decided_by: str,
    ) -> ApprovalRecord:
        decided_at = datetime.now(UTC)
        result = self._session.execute(
            update(ApprovalRecord)
            .where(
                ApprovalRecord.approval_id == approval_id,
                ApprovalRecord.tenant_id == tenant_id,
                ApprovalRecord.status == "pending",
            )
            .values(
                status=decision,
                decided_by=decided_by,
                decided_at=decided_at,
            )
        )
        self._session.flush()
        self._session.expire_all()
        record = self.get(approval_id=approval_id, tenant_id=tenant_id)
        if result.rowcount == 1 and record is not None:
            return record
        if record is None:
            raise ApprovalNotFoundError(f"Approval not found: {approval_id}")
        if record.status == decision:
            return record
        raise ApprovalConflictError(
            f"Approval {approval_id} was already decided as {record.status}."
        )


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
