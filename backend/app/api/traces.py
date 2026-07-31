from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.harness.tracing import TraceArtifact, TraceEvent, get_trace_store
from app.tools.redaction import redact_sensitive_data, sanitize_trace_error
from app.security.dependencies import require_permission
from app.security.principal import Principal
from app.storage.database import get_db
from app.storage.models import AgentRun

router = APIRouter(prefix="/api/traces", tags=["traces"])


@router.get("/{trace_id}")
def get_trace(
    trace_id: str,
    principal: Principal = Depends(require_permission("trace:read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if "*" not in principal.scopes and "trace:read:any" not in principal.scopes:
        owned_run_id = db.scalar(
            select(AgentRun.run_id)
            .where(
                AgentRun.trace_id == trace_id,
                AgentRun.tenant_id == principal.tenant_id,
            )
            .limit(1)
        )
        if owned_run_id is None:
            raise HTTPException(status_code=404, detail={"code": "trace_not_found"})
    store = get_trace_store()
    events = store.list_events(trace_id)
    if not events:
        raise HTTPException(status_code=404, detail={"code": "trace_not_found"})
    artifacts = store.list_artifacts(trace_id)
    return {
        "trace_id": trace_id,
        "events": [_event_to_dict(event) for event in events],
        "artifacts": {
            artifact_ref: _artifact_to_dict(artifact)
            for artifact_ref, artifact in artifacts.items()
        },
    }


def _event_to_dict(event: TraceEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "trace_id": event.trace_id,
        "node_name": event.node_name,
        "status": event.status,
        "latency_ms": event.latency_ms,
        "input_ref": event.input_ref,
        "output_ref": event.output_ref,
        "error": sanitize_trace_error(event.error),
        "metadata": redact_sensitive_data(event.metadata),
    }


def _artifact_to_dict(artifact: TraceArtifact) -> dict[str, Any]:
    return {
        "trace_id": artifact.trace_id,
        "node_name": artifact.node_name,
        "artifact_type": artifact.artifact_type,
        "payload": redact_sensitive_data(artifact.payload),
    }
