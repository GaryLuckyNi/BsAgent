from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import uuid4

from langgraph.store.base import BaseStore
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

    def list_for_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str | None = None,
        limit: int | None = None,
    ) -> list[Any]:
        raise NotImplementedError


@dataclass(frozen=True)
class ExperienceEntry:
    source_candidate_id: str
    tenant_id: str
    user_id: str
    memory_type: str
    memory_key: str
    content: str
    sensitivity: str
    source: str | None


class LangGraphExperienceStore:
    def __init__(self, store: BaseStore, *, recall_limit: int = 8) -> None:
        self._store = store
        self._recall_limit = recall_limit

    def save(self, payload: ExperienceCommitPayload) -> None:
        memory_key = payload.memory_key or payload.candidate_id
        self._store.put(
            _experience_namespace(payload.tenant_id, payload.user_id),
            memory_key,
            {
                "source_candidate_id": payload.candidate_id,
                "tenant_id": payload.tenant_id,
                "user_id": payload.user_id,
                "memory_type": payload.memory_type,
                "memory_key": memory_key,
                "content": payload.content,
                "sensitivity": payload.sensitivity,
                "source": payload.source,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    def list_for_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str | None = None,
        limit: int | None = None,
    ) -> list[ExperienceEntry]:
        del query
        items = self._store.search(
            _experience_namespace(tenant_id, user_id),
            limit=min(limit or self._recall_limit, self._recall_limit),
        )
        return [_experience_entry(item.value) for item in items]

    def forget_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_type: Literal["conversation", "user_preference"] | None = None,
    ) -> int:
        namespace = _experience_namespace(tenant_id, user_id)
        items = self._store.search(namespace, limit=10_000)
        selected = [
            item
            for item in items
            if memory_type is None or item.value.get("memory_type") == memory_type
        ]
        for item in selected:
            self._store.delete(namespace, item.key)
        return len(selected)


def _experience_namespace(tenant_id: str, user_id: str) -> tuple[str, ...]:
    return ("tenants", tenant_id, "users", user_id, "experiences")


def _experience_entry(value: dict[str, Any]) -> ExperienceEntry:
    return ExperienceEntry(
        source_candidate_id=str(value["source_candidate_id"]),
        tenant_id=str(value["tenant_id"]),
        user_id=str(value["user_id"]),
        memory_type=str(value["memory_type"]),
        memory_key=str(value["memory_key"]),
        content=str(value["content"]),
        sensitivity=str(value.get("sensitivity", "low")),
        source=value.get("source"),
    )


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
        query: str | None = None,
        limit: int | None = None,
    ) -> list[ExperienceEntryRecord]:
        del query
        entries = list(
            self._session.scalars(
                select(ExperienceEntryRecord)
                .where(ExperienceEntryRecord.tenant_id == tenant_id)
                .where(ExperienceEntryRecord.user_id == user_id)
                .order_by(ExperienceEntryRecord.created_at.desc())
                .limit(limit)
            )
        )
        entries.reverse()
        return entries

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
