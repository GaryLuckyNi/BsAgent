from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from app.harness.query import TraceQueryRepository
from app.security.dependencies import require_permission
from app.security.principal import Principal
from app.storage.database import get_db
from app.storage.models import AgentTraceArtifact, AgentTraceEvent
from app.tools.redaction import redact_sensitive_data, sanitize_trace_error

router = APIRouter(prefix="/api/traces", tags=["traces"])

TraceId = Annotated[
    str,
    Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]
TenantId = Annotated[
    str | None,
    Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]


@router.get("/{trace_id}")
def get_trace(
    trace_id: TraceId,
    include_artifacts: bool = Query(default=False),
    event_limit: int = Query(default=200, ge=1, le=500),
    artifact_limit: int = Query(default=20, ge=1, le=100),
    tenant_id: TenantId = None,
    principal: Principal = Depends(require_permission("trace:read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    target_tenant_id = _target_tenant_id(principal=principal, requested=tenant_id)
    view = TraceQueryRepository(db).get_trace(
        trace_id=trace_id,
        tenant_id=target_tenant_id,
        include_artifacts=include_artifacts,
        event_limit=event_limit,
        artifact_limit=artifact_limit,
    )
    if view is None:
        raise _trace_not_found()

    artifacts = {
        artifact.artifact_id: _artifact_to_dict(artifact)
        for artifact in view.artifacts
    }
    artifact_refs_by_event = _artifact_refs_by_event(view.artifacts)
    return {
        "trace_id": view.run.trace_id,
        "capture_status": view.run.trace_capture_status,
        "capture_level": view.run.trace_capture_level,
        "events": [
            _event_to_dict(event, artifact_refs_by_event.get(event.event_id, {}))
            for event in view.events
        ],
        "artifacts": artifacts,
    }


def _target_tenant_id(*, principal: Principal, requested: str | None) -> str:
    can_read_any = "*" in principal.scopes or "trace:read:any" in principal.scopes
    if requested is None:
        return principal.tenant_id
    if can_read_any:
        return requested
    if requested != principal.tenant_id:
        raise _trace_not_found()
    raise HTTPException(
        status_code=403,
        detail={"code": "permission_denied", "permission": "trace:read:any"},
    )


def _trace_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "trace_not_found"})


def _artifact_refs_by_event(
    artifacts: tuple[AgentTraceArtifact, ...],
) -> dict[str, dict[str, str]]:
    refs: dict[str, dict[str, str]] = {}
    for artifact in artifacts:
        field = (
            "input_ref"
            if artifact.artifact_type == "input_summary"
            else "output_ref"
        )
        refs.setdefault(artifact.event_id, {})[field] = artifact.artifact_id
    return refs


def _event_to_dict(
    event: AgentTraceEvent,
    artifact_refs: dict[str, str],
) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "trace_id": event.trace_id,
        "run_id": event.run_id,
        "node_name": event.node_name,
        "event_type": event.event_type,
        "status": event.status,
        "latency_ms": event.latency_ms,
        "input_ref": artifact_refs.get("input_ref"),
        "output_ref": artifact_refs.get("output_ref"),
        "error": sanitize_trace_error(event.error),
        "metadata": redact_sensitive_data(event.metadata_json),
        "capture_level": event.capture_level,
        "created_at": event.created_at,
    }


def _artifact_to_dict(artifact: AgentTraceArtifact) -> dict[str, Any]:
    return {
        "trace_id": artifact.trace_id,
        "run_id": artifact.run_id,
        "event_id": artifact.event_id,
        "artifact_type": artifact.artifact_type,
        "payload": redact_sensitive_data(artifact.payload),
        "capture_level": artifact.capture_level,
        "created_at": artifact.created_at,
    }
