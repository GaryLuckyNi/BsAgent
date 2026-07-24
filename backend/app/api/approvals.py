import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.approvals.repository import (
    ApprovalConflictError,
    ApprovalNotFoundError,
    ApprovalRepository,
)
from app.core.config import settings
from app.graph.checkpoints import build_graph_checkpointer
from app.graph.runtime import resume_question_answer_graph
from app.harness.tracing import get_trace_store
from app.llm.factory import maybe_build_llm_client
from app.memory.factory import build_experience_store
from app.memory.retriever import ExperienceMemoryRetriever
from app.security.rbac import RbacProvider, get_rbac_provider
from app.storage.database import get_db
from app.storage.models import AgentRun, ApprovalRecord
from app.storage.repositories import persist_agent_run

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class ApprovalDecisionRequest(BaseModel):
    tenant_id: str
    actor_id: str
    decision: Literal["approved", "rejected"]


@router.get("/pending")
def list_pending_approvals(
    tenant_id: str,
    actor_id: str,
    db: Session = Depends(get_db),
) -> dict[str, list[dict]]:
    rbac = get_rbac_provider()
    _require_permission(
        rbac=rbac,
        tenant_id=tenant_id,
        actor_id=actor_id,
        permission="approval:read",
    )
    records = ApprovalRepository(db).list_pending(tenant_id=tenant_id)
    return {"items": [_approval_payload(record) for record in records]}


@router.post("/{approval_id}/decision")
def decide_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    db: Session = Depends(get_db),
) -> dict:
    rbac = get_rbac_provider()
    _require_permission(
        rbac=rbac,
        tenant_id=request.tenant_id,
        actor_id=request.actor_id,
        permission="approval:decide",
    )
    repository = ApprovalRepository(db)
    existing = repository.get(
        approval_id=approval_id,
        tenant_id=request.tenant_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "approval_not_found"},
        )
    if existing.status == request.decision:
        persisted_run = db.get(AgentRun, existing.run_id)
        return {
            "approval": _approval_payload(existing),
            "run_id": existing.run_id,
            "run_status": (
                persisted_run.status if persisted_run is not None else existing.status
            ),
            "final_response": None,
        }
    try:
        record = repository.decide(
            approval_id=approval_id,
            tenant_id=request.tenant_id,
            decision=request.decision,
            decided_by=request.actor_id,
        )
        db.commit()
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "approval_not_found"}) from exc
    except ApprovalConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "approval_already_decided", "message": str(exc)},
        ) from exc

    request_principal = rbac.resolve_principal(
        tenant_id=record.tenant_id,
        user_id=record.request_user_id,
    )
    trace_store = get_trace_store()
    persisted_run = db.get(AgentRun, record.run_id)
    trace_id = persisted_run.trace_id if persisted_run is not None else None
    existing_event_count = len(trace_store.list_events(trace_id)) if trace_id else 0
    experience_store = build_experience_store(session=db)
    state = resume_question_answer_graph(
        checkpoint_thread_id=record.checkpoint_thread_id,
        approval_decision={
            "approval_id": record.approval_id,
            "arguments_hash": record.arguments_hash,
            "decision": record.status,
        },
        mock_evidence=settings.use_mock_rag,
        memory_retriever=ExperienceMemoryRetriever(experience_store),
        trace_store=trace_store,
        checkpointer=build_graph_checkpointer(),
        llm_client=maybe_build_llm_client(),
        permission_scopes=list(rbac.tool_scopes(request_principal)),
    )
    new_events = trace_store.list_events(state["trace_id"])[existing_event_count:]
    interrupt_payloads = [
        interrupt.value for interrupt in state.get("__interrupt__", [])
    ]
    for payload in interrupt_payloads:
        repository.create_pending_from_interrupt(payload)
    interrupted = bool(interrupt_payloads)
    run_status = "interrupted" if interrupted else (
        "rejected" if request.decision == "rejected" else "completed"
    )
    persist_agent_run(
        session=db,
        state=state,
        trace_events=new_events,
        status=run_status,
        experience_store=experience_store,
    )
    return {
        "approval": _approval_payload(record),
        "run_id": state["request_id"],
        "run_status": run_status,
        "final_response": state["final_response"],
    }


def _require_permission(
    *,
    rbac: RbacProvider,
    tenant_id: str,
    actor_id: str,
    permission: str,
) -> None:
    principal = rbac.resolve_principal(tenant_id=tenant_id, user_id=actor_id)
    if not rbac.has_permission(principal, permission):
        raise HTTPException(
            status_code=403,
            detail={"code": "permission_denied", "permission": permission},
        )


def _approval_payload(record: ApprovalRecord) -> dict:
    return {
        "approval_id": record.approval_id,
        "run_id": record.run_id,
        "session_id": record.session_id,
        "tenant_id": record.tenant_id,
        "request_user_id": record.request_user_id,
        "approval_type": record.approval_type,
        "tool_name": record.tool_name,
        "tool_version": record.tool_version,
        "arguments_hash": record.arguments_hash,
        "payload": json.loads(record.payload_json),
        "checkpoint_thread_id": record.checkpoint_thread_id,
        "status": record.status,
        "decided_by": record.decided_by,
        "created_at": record.created_at.isoformat(),
        "decided_at": record.decided_at.isoformat() if record.decided_at else None,
    }
