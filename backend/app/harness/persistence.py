import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.harness.capture_policy import CaptureDecision
from app.harness.trace_buffer import TraceArtifactDTO, TraceBuffer
from app.harness.tracing import TraceEvent
from app.storage.models import AgentRun, AgentTraceArtifact, AgentTraceEvent
from app.tools.redaction import redact_sensitive_data, sanitize_trace_error

logger = logging.getLogger(__name__)

TracePersistenceStatus = Literal["not_sampled", "captured", "persist_failed"]


class SessionFactory(Protocol):
    def begin(self): ...


@dataclass(frozen=True)
class TraceRunIdentity:
    run_id: str
    trace_id: str
    session_key: str
    tenant_id: str
    user_id: str


@dataclass(frozen=True)
class TracePersistenceResult:
    status: TracePersistenceStatus
    event_count: int = 0
    artifact_count: int = 0
    error_code: str | None = None


class TracePersistenceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TracePersistenceService:
    def __init__(self, *, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def persist_best_effort(
        self,
        *,
        run: TraceRunIdentity,
        buffer: TraceBuffer,
        decision: CaptureDecision,
    ) -> TracePersistenceResult:
        try:
            with self._session_factory.begin() as session:
                stored_run = _require_matching_run(session, run)
                if decision.status == "not_sampled":
                    stored_run.trace_capture_status = "not_sampled"
                    stored_run.trace_capture_level = None
                    return TracePersistenceResult(status="not_sampled")

                capture_level = _require_capture_level(decision)
                _validate_buffer_identity(run=run, buffer=buffer)
                event_count = _upsert_events(
                    session=session,
                    run=run,
                    events=buffer.events,
                    capture_level=capture_level,
                )
                artifact_count = 0
                if capture_level == "detailed":
                    artifact_count = _upsert_artifacts(
                        session=session,
                        run=run,
                        events=buffer.events,
                        artifacts=buffer.artifacts,
                        capture_level=capture_level,
                    )
                stored_run.trace_capture_status = "captured"
                stored_run.trace_capture_level = capture_level
            return TracePersistenceResult(
                status="captured",
                event_count=event_count,
                artifact_count=artifact_count,
            )
        except Exception as exc:
            error_code = _safe_error_code(exc)
            logger.error(
                "trace_persistence_failed code=%s run_fingerprint=%s",
                error_code,
                _fingerprint(run.run_id),
            )
            if error_code != "trace_run_identity_conflict":
                self._mark_failed_best_effort(run)
            return TracePersistenceResult(
                status="persist_failed",
                error_code=error_code,
            )

    def _mark_failed_best_effort(self, run: TraceRunIdentity) -> None:
        try:
            with self._session_factory.begin() as session:
                stored_run = _require_matching_run(session, run)
                stored_run.trace_capture_status = "persist_failed"
                stored_run.trace_capture_level = None
        except Exception as exc:
            logger.error(
                "trace_persistence_status_update_failed code=%s run_fingerprint=%s",
                _safe_error_code(exc),
                _fingerprint(run.run_id),
            )


def _require_matching_run(session: Session, identity: TraceRunIdentity) -> AgentRun:
    run = session.get(AgentRun, identity.run_id)
    if run is None:
        raise TracePersistenceError("trace_run_not_found")
    expected = (
        identity.trace_id,
        identity.session_key,
        identity.tenant_id,
        identity.user_id,
    )
    actual = (run.trace_id, run.session_key, run.tenant_id, run.user_id)
    if actual != expected:
        raise TracePersistenceError("trace_run_identity_conflict")
    return run


def _require_capture_level(
    decision: CaptureDecision,
) -> Literal["minimal", "detailed"]:
    if decision.status != "captured" or decision.level not in {
        "minimal",
        "detailed",
    }:
        raise TracePersistenceError("trace_capture_decision_invalid")
    return decision.level


def _validate_buffer_identity(*, run: TraceRunIdentity, buffer: TraceBuffer) -> None:
    for event in buffer.events:
        if event.trace_id != run.trace_id:
            raise TracePersistenceError("trace_event_identity_conflict")

    artifact_owners: dict[str, str] = {}
    for event in buffer.events:
        for artifact_ref in (event.input_ref, event.output_ref):
            if artifact_ref is None:
                continue
            owner = artifact_owners.setdefault(artifact_ref, event.event_id)
            if owner != event.event_id:
                raise TracePersistenceError("trace_artifact_identity_conflict")
    if any(artifact.artifact_ref not in artifact_owners for artifact in buffer.artifacts):
        raise TracePersistenceError("trace_artifact_identity_conflict")


def _upsert_events(
    *,
    session: Session,
    run: TraceRunIdentity,
    events: tuple[TraceEvent, ...],
    capture_level: Literal["minimal", "detailed"],
) -> int:
    event_ids = {event.event_id for event in events}
    existing = (
        {
            event.event_id: event
            for event in session.scalars(
                select(AgentTraceEvent).where(AgentTraceEvent.event_id.in_(event_ids))
            )
        }
        if event_ids
        else {}
    )

    for event in events:
        values = _event_values(run=run, event=event, capture_level=capture_level)
        stored = existing.get(event.event_id)
        if stored is not None:
            _validate_existing_event(stored=stored, values=values)
            continue
        try:
            with session.begin_nested():
                stored = AgentTraceEvent(**values)
                session.add(stored)
                session.flush()
        except IntegrityError:
            stored = session.scalar(
                select(AgentTraceEvent).where(AgentTraceEvent.event_id == event.event_id)
            )
            if stored is None:
                raise
            _validate_existing_event(stored=stored, values=values)
        existing[event.event_id] = stored
    return len(existing)


def _event_values(
    *,
    run: TraceRunIdentity,
    event: TraceEvent,
    capture_level: Literal["minimal", "detailed"],
) -> dict:
    metadata = redact_sensitive_data(event.metadata)
    if not isinstance(metadata, dict):
        raise TracePersistenceError("trace_event_metadata_invalid")
    return {
        "event_id": event.event_id,
        "trace_id": run.trace_id,
        "run_id": run.run_id,
        "node_name": event.node_name,
        "event_type": "node",
        "status": event.status,
        "latency_ms": event.latency_ms,
        "error": sanitize_trace_error(event.error),
        "metadata_json": metadata,
        "capture_level": capture_level,
    }


def _validate_existing_event(*, stored: AgentTraceEvent, values: dict) -> None:
    if any(getattr(stored, field) != value for field, value in values.items()):
        raise TracePersistenceError("trace_event_stable_id_conflict")


def _upsert_artifacts(
    *,
    session: Session,
    run: TraceRunIdentity,
    events: tuple[TraceEvent, ...],
    artifacts: tuple[TraceArtifactDTO, ...],
    capture_level: Literal["minimal", "detailed"],
) -> int:
    event_by_artifact = {
        artifact_ref: event.event_id
        for event in events
        for artifact_ref in (event.input_ref, event.output_ref)
        if artifact_ref is not None
    }
    artifact_ids = {artifact.artifact_ref for artifact in artifacts}
    existing = (
        {
            artifact.artifact_id: artifact
            for artifact in session.scalars(
                select(AgentTraceArtifact).where(AgentTraceArtifact.artifact_id.in_(artifact_ids))
            )
        }
        if artifact_ids
        else {}
    )

    for artifact in artifacts:
        values = _artifact_values(
            run=run,
            artifact=artifact,
            event_id=event_by_artifact[artifact.artifact_ref],
            capture_level=capture_level,
        )
        stored = existing.get(artifact.artifact_ref)
        if stored is not None:
            _validate_existing_artifact(stored=stored, values=values)
            continue
        try:
            with session.begin_nested():
                stored = AgentTraceArtifact(
                    **values,
                    expires_at=datetime.now(UTC) + timedelta(days=settings.trace_retention_days),
                )
                session.add(stored)
                session.flush()
        except IntegrityError:
            stored = session.get(AgentTraceArtifact, artifact.artifact_ref)
            if stored is None:
                raise
            _validate_existing_artifact(stored=stored, values=values)
        existing[artifact.artifact_ref] = stored
    return len(existing)


def _artifact_values(
    *,
    run: TraceRunIdentity,
    artifact: TraceArtifactDTO,
    event_id: str,
    capture_level: Literal["minimal", "detailed"],
) -> dict:
    try:
        payload = json.loads(artifact.payload_json)
    except (TypeError, ValueError) as exc:
        raise TracePersistenceError("trace_artifact_payload_invalid") from exc
    if not isinstance(payload, dict):
        raise TracePersistenceError("trace_artifact_payload_invalid")
    return {
        "artifact_id": artifact.artifact_ref,
        "trace_id": run.trace_id,
        "run_id": run.run_id,
        "event_id": event_id,
        "artifact_type": artifact.artifact_type,
        "payload": payload,
        "size_bytes": artifact.size_bytes,
        "capture_level": capture_level,
    }


def _validate_existing_artifact(
    *,
    stored: AgentTraceArtifact,
    values: dict,
) -> None:
    if any(getattr(stored, field) != value for field, value in values.items()):
        raise TracePersistenceError("trace_artifact_stable_id_conflict")


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, TracePersistenceError):
        return exc.code
    return "trace_persistence_failed"


def _fingerprint(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:16]
