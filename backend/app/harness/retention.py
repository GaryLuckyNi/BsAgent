from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, exists, select
from sqlalchemy.orm import Session

from app.storage.models import AgentTraceArtifact, AgentTraceEvent


MAX_CLEANUP_BATCH_SIZE = 10_000


@dataclass(frozen=True, slots=True)
class TraceRetentionResult:
    deleted_artifacts: int
    deleted_events: int

    @property
    def deleted_total(self) -> int:
        return self.deleted_artifacts + self.deleted_events


class TraceRetentionService:
    def __init__(self, session: Session, *, retention_days: int) -> None:
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or not 1 <= retention_days <= 365
        ):
            raise ValueError("retention_days must be an integer between 1 and 365")
        self._session = session
        self._retention_days = retention_days

    def purge_expired(
        self,
        *,
        limit: int,
        now: datetime | None = None,
    ) -> TraceRetentionResult:
        self._validate_limit(limit)
        selected_now = now or datetime.now(UTC)
        self._validate_utc(selected_now)

        artifact_ids = list(
            self._session.scalars(
                select(AgentTraceArtifact.artifact_id)
                .where(AgentTraceArtifact.expires_at <= selected_now)
                .order_by(
                    AgentTraceArtifact.expires_at.asc(),
                    AgentTraceArtifact.artifact_id.asc(),
                )
                .limit(limit)
            )
        )
        deleted_artifacts = self._delete_artifacts(artifact_ids)

        remaining = limit - deleted_artifacts
        if remaining == 0:
            return TraceRetentionResult(
                deleted_artifacts=deleted_artifacts,
                deleted_events=0,
            )

        event_cutoff = selected_now - timedelta(days=self._retention_days)
        artifact_reference = exists().where(
            and_(
                AgentTraceArtifact.event_id == AgentTraceEvent.event_id,
                AgentTraceArtifact.trace_id == AgentTraceEvent.trace_id,
                AgentTraceArtifact.run_id == AgentTraceEvent.run_id,
            )
        )
        event_ids = list(
            self._session.scalars(
                select(AgentTraceEvent.id)
                .where(
                    AgentTraceEvent.created_at <= event_cutoff,
                    ~artifact_reference,
                )
                .order_by(
                    AgentTraceEvent.created_at.asc(),
                    AgentTraceEvent.id.asc(),
                )
                .limit(remaining)
            )
        )
        deleted_events = self._delete_events(event_ids)
        return TraceRetentionResult(
            deleted_artifacts=deleted_artifacts,
            deleted_events=deleted_events,
        )

    def _delete_artifacts(self, artifact_ids: list[str]) -> int:
        if not artifact_ids:
            return 0
        result = self._session.execute(
            delete(AgentTraceArtifact).where(
                AgentTraceArtifact.artifact_id.in_(artifact_ids)
            )
        )
        return result.rowcount

    def _delete_events(self, event_ids: list[int]) -> int:
        if not event_ids:
            return 0
        result = self._session.execute(
            delete(AgentTraceEvent).where(AgentTraceEvent.id.in_(event_ids))
        )
        return result.rowcount

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_CLEANUP_BATCH_SIZE
        ):
            raise ValueError(
                f"limit must be an integer between 1 and {MAX_CLEANUP_BATCH_SIZE}"
            )

    @staticmethod
    def _validate_utc(now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now must be UTC-aware")
