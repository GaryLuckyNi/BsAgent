from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.storage.models import AgentRun, AgentTraceArtifact, AgentTraceEvent


MAX_EVENT_LIMIT = 500
MAX_ARTIFACT_LIMIT = 100


@dataclass(frozen=True)
class TraceView:
    run: AgentRun
    events: tuple[AgentTraceEvent, ...]
    artifacts: tuple[AgentTraceArtifact, ...]


class TraceQueryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_trace(
        self,
        *,
        trace_id: str,
        tenant_id: str,
        include_artifacts: bool = False,
        event_limit: int = 200,
        artifact_limit: int = 20,
    ) -> TraceView | None:
        run = self._session.scalar(
            select(AgentRun)
            .where(
                AgentRun.trace_id == trace_id,
                AgentRun.tenant_id == tenant_id,
            )
            .order_by(AgentRun.created_at, AgentRun.run_id)
            .limit(1)
        )
        if run is None:
            return None
        if run.trace_capture_status != "captured":
            return TraceView(run=run, events=(), artifacts=())

        events = tuple(
            self._session.scalars(
                select(AgentTraceEvent)
                .where(
                    AgentTraceEvent.run_id == run.run_id,
                    AgentTraceEvent.trace_id == run.trace_id,
                )
                .order_by(AgentTraceEvent.id)
                .limit(_bounded_limit(event_limit, MAX_EVENT_LIMIT))
            )
        )
        artifacts: tuple[AgentTraceArtifact, ...] = ()
        if include_artifacts:
            owned_event_ids = select(AgentTraceEvent.event_id).where(
                AgentTraceEvent.run_id == run.run_id,
                AgentTraceEvent.trace_id == run.trace_id,
            )
            artifacts = tuple(
                self._session.scalars(
                    select(AgentTraceArtifact)
                    .where(
                        AgentTraceArtifact.run_id == run.run_id,
                        AgentTraceArtifact.trace_id == run.trace_id,
                        AgentTraceArtifact.event_id.in_(owned_event_ids),
                    )
                    .order_by(
                        AgentTraceArtifact.created_at,
                        AgentTraceArtifact.artifact_id,
                    )
                    .limit(_bounded_limit(artifact_limit, MAX_ARTIFACT_LIMIT))
                )
            )
        return TraceView(run=run, events=events, artifacts=artifacts)


def _bounded_limit(value: int, maximum: int) -> int:
    return max(0, min(value, maximum))
