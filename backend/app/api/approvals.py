import json
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.approvals.repository import (
    ApprovalConflictError,
    ApprovalNotFoundError,
    ApprovalRepository,
    ApprovalResumeInProgressError,
)
from app.core.config import settings
from app.graph.checkpoints import build_graph_checkpointer
from app.graph.runtime import AgentGraphExecutionError, resume_question_answer_graph
from app.graph.state import AgentState
from app.harness.tracing import get_trace_store
from app.llm.factory import maybe_build_llm_client
from app.security.dependencies import require_permission
from app.security.principal import Principal
from app.storage.database import get_db
from app.storage.models import AgentRun, ApprovalRecord
from app.storage.repositories import persist_agent_run
from app.tools.redaction import redact_sensitive_data

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]


@dataclass(frozen=True)
class ApprovalResumeSnapshot:
    approval_id: str
    run_id: str
    session_id: str
    session_key: str
    tenant_id: str
    request_user_id: str
    trace_id: str
    arguments_hash: str
    checkpoint_thread_id: str
    checkpoint_namespace: Literal[""]
    claim_token: str


class ApprovalResumeIdentityError(RuntimeError):
    code = "approval_resume_identity_mismatch"


@router.get("/pending")
def list_pending_approvals(
    principal: Principal = Depends(require_permission("approval:read")),
    db: Session = Depends(get_db),
) -> dict[str, list[dict]]:
    records = ApprovalRepository(db).list_pending(tenant_id=principal.tenant_id)
    return {"items": [_approval_payload(record) for record in records]}


@router.post("/{approval_id}/decision")
def decide_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    principal: Principal = Depends(require_permission("approval:decide")),
    db: Session = Depends(get_db),
) -> dict:
    repository = ApprovalRepository(db)
    try:
        claim = repository.claim_resume(
            approval_id=approval_id,
            tenant_id=principal.tenant_id,
            decision=request.decision,
            decided_by=principal.user_id,
        )
        approval_payload = _approval_payload(claim.record)
        persisted_run = db.get(AgentRun, claim.record.run_id)
        resume_snapshot = (
            _resume_snapshot(claim.record, claim.claim_token, persisted_run)
            if claim.acquired
            else None
        )
        db.commit()
    except ApprovalNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail={"code": "approval_not_found"}) from exc
    except ApprovalResumeInProgressError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "approval_resume_in_progress", "message": str(exc)},
        ) from exc
    except ApprovalConflictError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "approval_already_decided", "message": str(exc)},
        ) from exc
    except ApprovalResumeIdentityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code},
        ) from exc

    if not claim.acquired:
        persisted_run = db.get(AgentRun, approval_payload["run_id"])
        return {
            "approval": approval_payload,
            "run_id": approval_payload["run_id"],
            "run_status": (
                persisted_run.status
                if persisted_run is not None
                else approval_payload["status"]
            ),
            "final_response": None,
        }

    assert resume_snapshot is not None
    trace_store = get_trace_store()
    try:
        state = resume_question_answer_graph(
            checkpoint_thread_id=resume_snapshot.checkpoint_thread_id,
            checkpoint_namespace=resume_snapshot.checkpoint_namespace,
            approval_decision={
                "approval_id": resume_snapshot.approval_id,
                "arguments_hash": resume_snapshot.arguments_hash,
                "decision": request.decision,
            },
            mock_evidence=settings.use_mock_rag,
            trace_store=trace_store,
            checkpointer=build_graph_checkpointer(),
            llm_client=maybe_build_llm_client(),
            permission_scopes=None,
        )
    except AgentGraphExecutionError as exc:
        _record_resume_failure(
            db=db,
            repository=repository,
            snapshot=resume_snapshot,
            error=f"{exc.code}: {exc.message}",
        )
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "node": exc.node_name, "message": exc.message},
        ) from exc
    except Exception as exc:
        _record_resume_failure(
            db=db,
            repository=repository,
            snapshot=resume_snapshot,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "approval_resume_failed",
                "message": "审批已记录，但 checkpoint 恢复失败，可使用相同决定重试。",
            },
        ) from exc
    try:
        _validate_resume_state(resume_snapshot, state)
        trace_events = trace_store.list_events(state["trace_id"])
        interrupt_payloads = [
            interrupt.value for interrupt in state.get("__interrupt__", [])
        ]
        for payload in interrupt_payloads:
            repository.create_pending_from_interrupt(
                {
                    **payload,
                    "run_id": resume_snapshot.run_id,
                    "session_id": resume_snapshot.session_id,
                    "session_key": state["session_key"],
                    "tenant_id": resume_snapshot.tenant_id,
                    "request_user_id": resume_snapshot.request_user_id,
                    "checkpoint_namespace": resume_snapshot.checkpoint_namespace,
                }
            )
        interrupted = bool(interrupt_payloads)
        run_status = "interrupted" if interrupted else (
            "rejected" if request.decision == "rejected" else "completed"
        )
        persist_agent_run(
            session=db,
            state=state,
            trace_events=trace_events,
            status=run_status,
            commit=False,
            run_id=resume_snapshot.run_id,
        )
        record = repository.complete_resume(
            approval_id=resume_snapshot.approval_id,
            tenant_id=resume_snapshot.tenant_id,
            claim_token=resume_snapshot.claim_token,
        )
        approval_payload = _approval_payload(record)
        db.commit()
    except ApprovalResumeIdentityError as exc:
        _record_resume_failure(
            db=db,
            repository=repository,
            snapshot=resume_snapshot,
            error=f"{exc.code}: {exc}",
        )
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code},
        ) from exc
    except Exception as exc:
        _record_resume_failure(
            db=db,
            repository=repository,
            snapshot=resume_snapshot,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "approval_resume_failed",
                "message": "审批恢复收尾失败，可使用相同决定重试。",
            },
        ) from exc
    return {
        "approval": approval_payload,
        "run_id": resume_snapshot.run_id,
        "run_status": run_status,
        "final_response": state["final_response"],
    }


def _record_resume_failure(
    *,
    db: Session,
    repository: ApprovalRepository,
    snapshot: ApprovalResumeSnapshot,
    error: str,
) -> None:
    db.rollback()
    repository.fail_resume(
        approval_id=snapshot.approval_id,
        tenant_id=snapshot.tenant_id,
        claim_token=snapshot.claim_token,
        error=error,
    )
    db.commit()


def _approval_payload(record: ApprovalRecord) -> dict:
    return {
        "approval_id": record.approval_id,
        "run_id": record.run_id,
        "session_id": record.session_id,
        "session_key": record.session_key,
        "tenant_id": record.tenant_id,
        "request_user_id": record.request_user_id,
        "approval_type": record.approval_type,
        "tool_name": record.tool_name,
        "tool_version": record.tool_version,
        "arguments_hash": record.arguments_hash,
        "payload": redact_sensitive_data(json.loads(record.payload_json)),
        "checkpoint_thread_id": record.checkpoint_thread_id,
        "checkpoint_namespace": json.loads(record.payload_json).get(
            "checkpoint_namespace", ""
        ),
        "status": record.status,
        "decision": record.decision,
        "resume_attempts": record.resume_attempts,
        "last_resume_error": record.last_resume_error,
        "resume_claimed_by": record.resume_claimed_by,
        "resume_claimed_at": (
            record.resume_claimed_at.isoformat() if record.resume_claimed_at else None
        ),
        "decided_by": record.decided_by,
        "created_at": record.created_at.isoformat(),
        "decided_at": record.decided_at.isoformat() if record.decided_at else None,
    }


def _resume_snapshot(
    record: ApprovalRecord,
    claim_token: str | None,
    run: AgentRun | None,
) -> ApprovalResumeSnapshot:
    if claim_token is None:
        raise ApprovalConflictError("Approval resume claim token is missing.")
    if run is None or (
        run.run_id != record.run_id
        or run.session_id != record.session_id
        or run.session_key != record.session_key
        or run.tenant_id != record.tenant_id
        or run.user_id != record.request_user_id
    ):
        raise ApprovalResumeIdentityError(
            "Approval resume Run identity is inconsistent."
        )
    checkpoint_namespace = json.loads(record.payload_json).get(
        "checkpoint_namespace", ""
    )
    if checkpoint_namespace != "":
        raise ApprovalResumeIdentityError(
            "Approval checkpoint namespace is invalid."
        )
    return ApprovalResumeSnapshot(
        approval_id=record.approval_id,
        run_id=record.run_id,
        session_id=record.session_id,
        session_key=record.session_key,
        tenant_id=record.tenant_id,
        request_user_id=record.request_user_id,
        trace_id=run.trace_id,
        arguments_hash=record.arguments_hash,
        checkpoint_thread_id=record.checkpoint_thread_id,
        checkpoint_namespace="",
        claim_token=claim_token,
    )


def _validate_resume_state(
    snapshot: ApprovalResumeSnapshot,
    state: AgentState,
) -> None:
    if (
        state["session_id"] != snapshot.session_id
        or state["session_key"] != snapshot.session_key
        or state["tenant_id"] != snapshot.tenant_id
        or state["user_id"] != snapshot.request_user_id
        or state["trace_id"] != snapshot.trace_id
    ):
        raise ApprovalResumeIdentityError(
            "Approval resume State identity does not match the persisted Run."
        )
