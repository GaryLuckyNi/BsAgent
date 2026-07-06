from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.schemas.memory import ExperienceCommitPayload
from app.storage.models import ExperienceEntryRecord


class ExperienceRetentionPolicy(BaseModel):
    tenant_id: str
    max_age_days: int | None = Field(default=None, ge=1)
    max_entries_per_user: int | None = Field(default=None, ge=1)
    memory_types: list[Literal["conversation", "user_preference"]] | None = None


class ExperienceStore(Protocol):
    def save(self, payload: ExperienceCommitPayload) -> None:
        raise NotImplementedError


class SqlAlchemyExperienceStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, payload: ExperienceCommitPayload) -> None:
        existing = self._session.scalars(
            select(ExperienceEntryRecord).where(
                ExperienceEntryRecord.source_candidate_id == payload.candidate_id
            )
        ).one_or_none()
        if existing is not None:
            return

        self._session.add(
            ExperienceEntryRecord(
                entry_id=f"exp_{uuid4().hex}",
                tenant_id=payload.tenant_id,
                user_id=payload.user_id,
                memory_type=payload.memory_type,
                content=payload.content,
                source_candidate_id=payload.candidate_id,
                sensitivity=payload.sensitivity,
                source=payload.source,
            )
        )

    def list_for_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> list[ExperienceEntryRecord]:
        return list(
            self._session.scalars(
                select(ExperienceEntryRecord)
                .where(ExperienceEntryRecord.tenant_id == tenant_id)
                .where(ExperienceEntryRecord.user_id == user_id)
                .order_by(ExperienceEntryRecord.created_at.asc())
            )
        )

    def forget_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_type: Literal["conversation", "user_preference"] | None = None,
    ) -> int:
        entries = self._session.scalars(
            _tenant_query(tenant_id, memory_type)
            .where(ExperienceEntryRecord.user_id == user_id)
            .order_by(ExperienceEntryRecord.created_at.asc())
        ).all()
        for entry in entries:
            self._session.delete(entry)
        return len(entries)

    def forget_tenant(
        self,
        *,
        tenant_id: str,
        memory_type: Literal["conversation", "user_preference"] | None = None,
    ) -> int:
        entries = self._session.scalars(
            _tenant_query(tenant_id, memory_type).order_by(ExperienceEntryRecord.created_at.asc())
        ).all()
        for entry in entries:
            self._session.delete(entry)
        return len(entries)

    def apply_retention_policy(
        self,
        policy: ExperienceRetentionPolicy,
        *,
        now: datetime | None = None,
    ) -> int:
        selected_now = now or datetime.now(UTC)
        deleted = 0
        if policy.max_age_days is not None:
            cutoff = selected_now - timedelta(days=policy.max_age_days)
            expired_entries = self._session.scalars(
                _policy_query(policy).where(ExperienceEntryRecord.created_at < cutoff)
            ).all()
            for entry in expired_entries:
                self._session.delete(entry)
            deleted += len(expired_entries)
            self._session.flush()

        if policy.max_entries_per_user is not None:
            active_entries = self._session.scalars(
                _policy_query(policy).order_by(
                    ExperienceEntryRecord.user_id.asc(),
                    ExperienceEntryRecord.created_at.desc(),
                )
            ).all()
            counts_by_user: dict[str, int] = {}
            for entry in active_entries:
                counts_by_user[entry.user_id] = counts_by_user.get(entry.user_id, 0) + 1
                if counts_by_user[entry.user_id] <= policy.max_entries_per_user:
                    continue
                self._session.delete(entry)
                deleted += 1

        return deleted


def _tenant_query(
    tenant_id: str,
    memory_type: Literal["conversation", "user_preference"] | None,
):
    query = select(ExperienceEntryRecord).where(ExperienceEntryRecord.tenant_id == tenant_id)
    if memory_type is not None:
        query = query.where(ExperienceEntryRecord.memory_type == memory_type)
    return query


def _policy_query(policy: ExperienceRetentionPolicy):
    query = select(ExperienceEntryRecord).where(
        ExperienceEntryRecord.tenant_id == policy.tenant_id
    )
    if policy.memory_types is not None:
        query = query.where(ExperienceEntryRecord.memory_type.in_(policy.memory_types))
    return query
