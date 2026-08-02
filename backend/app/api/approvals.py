import json
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.approvals.repository import (
    ApprovalConflictError,
    ApprovalNotFoundError,
    ApprovalRepository,
    ApprovalResumeInProgressError,
)
from app.core.config import settings
from app.conversations.repository import (
    ConversationConflictError,
    ConversationIntegrityError,
    ConversationNotFoundError,
)
from app.conversations.service import ConversationService
from app.graph.checkpoints import build_graph_checkpointer
from app.graph.runtime import AgentGraphExecutionError, resume_question_answer_graph
from app.graph.state import AgentState, build_checkpoint_thread_id
from app.harness.capture_policy import TraceCaptureOutcome
from app.harness.persistence import (
    TracePersistenceService,
    TraceRunIdentity,
    finalize_trace_best_effort,
)
from app.harness.trace_buffer import TraceBuffer
from app.llm.factory import maybe_build_llm_client
from app.security.dependencies import require_permission
from app.security.principal import Principal
from app.security.rbac import get_rbac_provider
from app.storage.database import get_db
from app.storage.models import AgentMessage, AgentRun, ApprovalRecord
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
    request_id: str
    assistant_message_id: str
    arguments_hash: str
    checkpoint_thread_id: str
    checkpoint_namespace: Literal[""]
    claim_token: str
    claim_generation: int


class ApprovalResumeIdentityError(RuntimeError):
    code = "approval_resume_identity_mismatch"


class ApprovalPersistenceUnavailable(RuntimeError):
    code = "approval_persistence_unavailable"


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
        pending_record = repository.get(
            approval_id=approval_id,
            tenant_id=principal.tenant_id,
        )
        if pending_record is None:
            raise ApprovalNotFoundError(f"Approval not found: {approval_id}")
        permission_scopes = _resolve_resume_permission_scopes(
            pending_record,
        )
        claim = repository.claim_resume(
            approval_id=approval_id,
            tenant_id=principal.tenant_id,
            decision=request.decision,
            decided_by=principal.user_id,
            claim_timeout=timedelta(
                seconds=settings.approval_resume_claim_timeout_seconds
            ),
        )
        approval_record_payload = _parse_approval_payload(claim.record)
        approval_payload = _approval_payload(
            claim.record,
            payload=approval_record_payload,
        )
        if claim.acquired:
            persisted_run = db.scalar(
                select(AgentRun).where(
                    AgentRun.tenant_id == claim.record.tenant_id,
                    AgentRun.run_id == claim.record.run_id,
                )
            )
            user_message, assistant_message = _load_resume_turn(db, claim.record)
            _validate_resume_turn_lifecycle(
                db=db,
                record=claim.record,
                user_message=user_message,
                assistant_message=assistant_message,
            )
            resume_snapshot = _resume_snapshot(
                claim.record,
                claim.claim_token,
                claim.claim_generation,
                persisted_run,
                user_message,
                assistant_message,
                approval_record_payload,
            )
        else:
            resume_snapshot = None
        db.commit()
    except SQLAlchemyError as exc:
        _raise_persistence_unavailable(db, exc)
    except ApprovalNotFoundError as exc:
        _rollback_safely(db)
        raise HTTPException(status_code=404, detail={"code": "approval_not_found"}) from exc
    except ApprovalResumeInProgressError as exc:
        _rollback_safely(db)
        raise HTTPException(
            status_code=409,
            detail={"code": "approval_resume_in_progress", "message": str(exc)},
        ) from exc
    except ApprovalConflictError as exc:
        _rollback_safely(db)
        raise HTTPException(
            status_code=409,
            detail={"code": "approval_already_decided", "message": str(exc)},
        ) from exc
    except ApprovalResumeIdentityError as exc:
        _rollback_safely(db)
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code},
        ) from exc

    if not claim.acquired:
        try:
            persisted_run = db.scalar(
                select(AgentRun).where(
                    AgentRun.tenant_id == principal.tenant_id,
                    AgentRun.run_id == approval_payload["run_id"],
                )
            )
            _, assistant_message = _load_resume_turn(db, claim.record)
            db.commit()
        except SQLAlchemyError as exc:
            _raise_persistence_unavailable(db, exc)
        except ApprovalResumeIdentityError as exc:
            _rollback_safely(db)
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code},
            ) from exc
        return {
            "approval": approval_payload,
            "run_id": approval_payload["run_id"],
            "run_status": (
                persisted_run.status
                if persisted_run is not None
                else approval_payload["status"]
            ),
            "request_id": assistant_message.request_id,
            "message_id": assistant_message.message_id,
            "final_response": _persisted_assistant_response(
                assistant_message,
                run=persisted_run,
            ),
        }

    assert resume_snapshot is not None
    trace_buffer = TraceBuffer()
    state: AgentState | None = None
    try:
        with _ApprovalResumeLease(
            db=db,
            snapshot=resume_snapshot,
            timeout_seconds=settings.approval_resume_claim_timeout_seconds,
        ) as lease:
            state = resume_question_answer_graph(
                checkpoint_thread_id=resume_snapshot.checkpoint_thread_id,
                checkpoint_namespace=resume_snapshot.checkpoint_namespace,
                approval_decision={
                    "approval_id": resume_snapshot.approval_id,
                    "arguments_hash": resume_snapshot.arguments_hash,
                    "decision": request.decision,
                },
                mock_evidence=settings.use_mock_rag,
                trace_buffer=trace_buffer,
                checkpointer=build_graph_checkpointer(),
                llm_client=maybe_build_llm_client(),
                permission_scopes=permission_scopes,
            )
            lease.ensure_owned()
    except ApprovalResumeInProgressError as exc:
        _finalize_resume_trace_best_effort(
            db=db,
            snapshot=resume_snapshot,
            buffer=trace_buffer,
            state=state,
            failed=True,
        )
        raise HTTPException(
            status_code=409,
            detail={"code": "approval_resume_claim_lost", "message": str(exc)},
        ) from exc
    except SQLAlchemyError as exc:
        try:
            _record_resume_failure(
                db=db,
                repository=repository,
                snapshot=resume_snapshot,
                error="approval_persistence_unavailable",
                cause=exc,
            )
        except ApprovalPersistenceUnavailable as persistence_exc:
            _raise_persistence_unavailable(db, persistence_exc)
        _finalize_resume_trace_best_effort(
            db=db,
            snapshot=resume_snapshot,
            buffer=trace_buffer,
            state=state,
            failed=True,
        )
        _raise_persistence_unavailable(db, exc)
    except AgentGraphExecutionError as exc:
        try:
            _record_resume_failure(
                db=db,
                repository=repository,
                snapshot=resume_snapshot,
                error=f"{exc.code}: {exc.message}",
                cause=exc,
            )
        except ApprovalPersistenceUnavailable as persistence_exc:
            _raise_persistence_unavailable(db, persistence_exc)
        _finalize_resume_trace_best_effort(
            db=db,
            snapshot=resume_snapshot,
            buffer=trace_buffer,
            state=state,
            failed=True,
        )
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "node": exc.node_name, "message": exc.message},
        ) from exc
    except Exception as exc:
        try:
            _record_resume_failure(
                db=db,
                repository=repository,
                snapshot=resume_snapshot,
                error=f"{type(exc).__name__}: {exc}",
                cause=exc,
            )
        except ApprovalPersistenceUnavailable as persistence_exc:
            _raise_persistence_unavailable(db, persistence_exc)
        _finalize_resume_trace_best_effort(
            db=db,
            snapshot=resume_snapshot,
            buffer=trace_buffer,
            state=state,
            failed=True,
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
        interrupt_payloads = [
            interrupt.value for interrupt in state.get("__interrupt__", [])
        ]
        for interrupt_payload in interrupt_payloads:
            repository.create_pending_from_interrupt(
                {
                    **interrupt_payload,
                    "run_id": resume_snapshot.run_id,
                    "session_id": resume_snapshot.session_id,
                    "session_key": state["session_key"],
                    "tenant_id": resume_snapshot.tenant_id,
                    "request_user_id": resume_snapshot.request_user_id,
                    "checkpoint_thread_id": resume_snapshot.checkpoint_thread_id,
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
            status=run_status,
            commit=False,
            run_id=resume_snapshot.run_id,
            update_run_status=False,
        )
        record = repository.complete_resume(
            approval_id=resume_snapshot.approval_id,
            tenant_id=resume_snapshot.tenant_id,
            claim_token=resume_snapshot.claim_token,
            claim_generation=resume_snapshot.claim_generation,
        )
        final_response = state["final_response"] or {}
        conversation_service = ConversationService(db)
        if not interrupted:
            if request.decision == "rejected":
                conversation_service.reject_turn(
                    tenant_id=resume_snapshot.tenant_id,
                    run_id=resume_snapshot.run_id,
                    content=final_response.get("content", ""),
                    metadata=_assistant_response_metadata(final_response),
                )
            else:
                conversation_service.complete_turn(
                    tenant_id=resume_snapshot.tenant_id,
                    run_id=resume_snapshot.run_id,
                    content=final_response.get("content", ""),
                    metadata=_assistant_response_metadata(final_response),
                )
        approval_payload = _approval_payload(
            record,
            payload=approval_record_payload,
        )
        db.commit()
    except SQLAlchemyError as exc:
        try:
            _record_resume_failure(
                db=db,
                repository=repository,
                snapshot=resume_snapshot,
                error="approval_persistence_unavailable",
                cause=exc,
            )
        except ApprovalPersistenceUnavailable as persistence_exc:
            _raise_persistence_unavailable(db, persistence_exc)
        _finalize_resume_trace_best_effort(
            db=db,
            snapshot=resume_snapshot,
            buffer=trace_buffer,
            state=state,
            failed=True,
        )
        _raise_persistence_unavailable(db, exc)
    except ApprovalResumeIdentityError as exc:
        try:
            _record_resume_failure(
                db=db,
                repository=repository,
                snapshot=resume_snapshot,
                error=f"{exc.code}: {exc}",
                cause=exc,
            )
        except ApprovalPersistenceUnavailable as persistence_exc:
            _raise_persistence_unavailable(db, persistence_exc)
        _finalize_resume_trace_best_effort(
            db=db,
            snapshot=resume_snapshot,
            buffer=trace_buffer,
            state=state,
            failed=True,
        )
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code},
        ) from exc
    except Exception as exc:
        try:
            _record_resume_failure(
                db=db,
                repository=repository,
                snapshot=resume_snapshot,
                error=f"{type(exc).__name__}: {exc}",
                cause=exc,
            )
        except ApprovalPersistenceUnavailable as persistence_exc:
            _raise_persistence_unavailable(db, persistence_exc)
        _finalize_resume_trace_best_effort(
            db=db,
            snapshot=resume_snapshot,
            buffer=trace_buffer,
            state=state,
            failed=True,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "approval_resume_failed",
                "message": "审批恢复收尾失败，可使用相同决定重试。",
            },
        ) from exc
    _finalize_resume_trace_best_effort(
        db=db,
        snapshot=resume_snapshot,
        buffer=trace_buffer,
        state=state,
        failed=False,
    )
    return {
        "approval": approval_payload,
        "run_id": resume_snapshot.run_id,
        "run_status": run_status,
        "request_id": resume_snapshot.request_id,
        "message_id": resume_snapshot.assistant_message_id,
        "final_response": state["final_response"],
    }


def _record_resume_failure(
    *,
    db: Session,
    repository: ApprovalRepository,
    snapshot: ApprovalResumeSnapshot,
    error: str,
    cause: Exception,
) -> None:
    _rollback_safely(db)
    try:
        repository.fail_resume(
            approval_id=snapshot.approval_id,
            tenant_id=snapshot.tenant_id,
            claim_token=snapshot.claim_token,
            claim_generation=snapshot.claim_generation,
            error=error,
        )
        db.commit()
    except SQLAlchemyError:
        _rollback_safely(db)
        raise ApprovalPersistenceUnavailable from cause


def _finalize_resume_trace_best_effort(
    *,
    db: Session,
    snapshot: ApprovalResumeSnapshot,
    buffer: TraceBuffer,
    state: AgentState | None,
    failed: bool,
) -> None:
    intent = (state or {}).get("intent") or {}
    verification = (state or {}).get("verification") or {}
    session_factory = sessionmaker(
        bind=db.get_bind(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    finalize_trace_best_effort(
        run=TraceRunIdentity(
            run_id=snapshot.run_id,
            trace_id=snapshot.trace_id,
            session_key=snapshot.session_key,
            tenant_id=snapshot.tenant_id,
            user_id=snapshot.request_user_id,
        ),
        buffer=buffer,
        outcome=TraceCaptureOutcome(
            failed=failed,
            risk_level=intent.get("risk_level"),
            approval_occurred=True,
            verifier_status=verification.get("verdict"),
        ),
        service=TracePersistenceService(session_factory=session_factory),
    )


def _approval_payload(
    record: ApprovalRecord,
    *,
    payload: dict | None = None,
) -> dict:
    parsed_payload = payload if payload is not None else _parse_approval_payload(record)
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
        "payload": redact_sensitive_data(parsed_payload),
        "checkpoint_thread_id": record.checkpoint_thread_id,
        "checkpoint_namespace": parsed_payload.get("checkpoint_namespace", ""),
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
    claim_generation: int | None,
    run: AgentRun | None,
    user_message: AgentMessage,
    assistant_message: AgentMessage,
    payload: dict,
) -> ApprovalResumeSnapshot:
    if claim_token is None or claim_generation is None:
        raise ApprovalConflictError("Approval resume claim identity is missing.")
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
    expected_payload_identity = {
        "run_id": record.run_id,
        "session_id": record.session_id,
        "session_key": record.session_key,
        "tenant_id": record.tenant_id,
        "request_user_id": record.request_user_id,
        "checkpoint_thread_id": record.checkpoint_thread_id,
        "checkpoint_namespace": "",
    }
    if any(
        payload.get(field) != expected
        for field, expected in expected_payload_identity.items()
    ):
        raise ApprovalResumeIdentityError(
            "Approval payload identity is inconsistent."
        )
    if (
        user_message.tenant_id != record.tenant_id
        or user_message.user_id != record.request_user_id
        or user_message.session_key != record.session_key
        or user_message.run_id != record.run_id
    ):
        raise ApprovalResumeIdentityError(
            "Approval resume User Message identity is inconsistent."
        )
    if (
        assistant_message.tenant_id != record.tenant_id
        or assistant_message.user_id != record.request_user_id
        or assistant_message.session_key != record.session_key
        or assistant_message.run_id != record.run_id
        or assistant_message.request_id != user_message.request_id
        or assistant_message.sequence_no != user_message.sequence_no + 1
        or assistant_message.status != "interrupted"
        or run.status != "interrupted"
    ):
        raise ApprovalResumeIdentityError(
            "Approval resume Assistant Message identity is inconsistent."
        )
    expected_checkpoint_thread_id = build_checkpoint_thread_id(
        tenant_id=record.tenant_id,
        session_key=record.session_key,
        request_id=user_message.request_id,
    )
    if (
        record.checkpoint_thread_id != expected_checkpoint_thread_id
        or payload["checkpoint_thread_id"] != expected_checkpoint_thread_id
        or payload["checkpoint_namespace"] != ""
    ):
        raise ApprovalResumeIdentityError(
            "Approval checkpoint identity is inconsistent."
        )
    return ApprovalResumeSnapshot(
        approval_id=record.approval_id,
        run_id=record.run_id,
        session_id=record.session_id,
        session_key=record.session_key,
        tenant_id=record.tenant_id,
        request_user_id=record.request_user_id,
        trace_id=run.trace_id,
        request_id=user_message.request_id,
        assistant_message_id=assistant_message.message_id,
        arguments_hash=record.arguments_hash,
        checkpoint_thread_id=record.checkpoint_thread_id,
        checkpoint_namespace="",
        claim_token=claim_token,
        claim_generation=claim_generation,
    )


def _resolve_resume_permission_scopes(
    record: ApprovalRecord,
) -> list[str]:
    try:
        provider = get_rbac_provider()
        request_principal = provider.resolve_principal(
            tenant_id=record.tenant_id,
            user_id=record.request_user_id,
        )
        if (
            request_principal.tenant_id != record.tenant_id
            or request_principal.user_id != record.request_user_id
        ):
            raise ValueError("RBAC principal identity mismatch")
        scopes = tuple(provider.tool_scopes(request_principal))
    except Exception as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "approval_requester_authorization_unavailable"},
        ) from exc
    return list(scopes)


class _ApprovalResumeLease:
    def __init__(
        self,
        *,
        db: Session,
        snapshot: ApprovalResumeSnapshot,
        timeout_seconds: int,
    ) -> None:
        self._session_factory = sessionmaker(
            bind=db.get_bind(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        self._snapshot = snapshot
        self._interval_seconds = max(1.0, timeout_seconds / 3)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._renew_loop,
            name=f"approval-lease-{snapshot.approval_id}",
            daemon=True,
        )

    def __enter__(self) -> "_ApprovalResumeLease":
        self._thread.start()
        return self

    def __exit__(self, *_args) -> None:
        self._stop.set()
        self._thread.join(timeout=min(self._interval_seconds, 1.0))

    def ensure_owned(self) -> None:
        if self._lost.is_set():
            raise ApprovalResumeInProgressError(
                f"Approval {self._snapshot.approval_id} resume claim was lost."
            )

    def _renew_loop(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                with self._session_factory() as lease_db:
                    ApprovalRepository(lease_db).renew_resume_claim(
                        approval_id=self._snapshot.approval_id,
                        tenant_id=self._snapshot.tenant_id,
                        claim_token=self._snapshot.claim_token,
                        claim_generation=self._snapshot.claim_generation,
                    )
                    lease_db.commit()
            except Exception:
                self._lost.set()
                return


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
        or state["request_id"] != snapshot.request_id
        or state["assistant_message_id"] != snapshot.assistant_message_id
    ):
        raise ApprovalResumeIdentityError(
            "Approval resume State identity does not match the persisted Run."
        )


def _load_resume_turn(
    db: Session,
    record: ApprovalRecord,
) -> tuple[AgentMessage, AgentMessage]:
    messages = list(
        db.scalars(
            select(AgentMessage).where(
                AgentMessage.tenant_id == record.tenant_id,
                AgentMessage.run_id == record.run_id,
                AgentMessage.session_key == record.session_key,
                AgentMessage.role.in_(("user", "assistant")),
            )
        )
    )
    by_role = {message.role: message for message in messages}
    if len(messages) != 2 or set(by_role) != {"user", "assistant"}:
        raise ApprovalResumeIdentityError(
            "Approval resume requires exactly one User and Assistant Message."
        )
    return by_role["user"], by_role["assistant"]


def _validate_resume_turn_lifecycle(
    *,
    db: Session,
    record: ApprovalRecord,
    user_message: AgentMessage,
    assistant_message: AgentMessage,
) -> None:
    try:
        handle = ConversationService(db).resume_turn(
            tenant_id=record.tenant_id,
            run_id=record.run_id,
        )
    except (
        ConversationConflictError,
        ConversationIntegrityError,
        ConversationNotFoundError,
    ) as exc:
        raise ApprovalResumeIdentityError(
            "Approval resume Turn lifecycle is inconsistent."
        ) from exc
    if (
        handle.request_id != user_message.request_id
        or handle.user_message_id != user_message.message_id
        or handle.assistant_message_id != assistant_message.message_id
    ):
        raise ApprovalResumeIdentityError(
            "Approval resume Turn handle is inconsistent."
        )


def _assistant_response_metadata(final_response: dict) -> dict:
    return {
        "cited_evidence_ids": list(final_response.get("cited_evidence_ids", [])),
        "approval_status": final_response.get("approval_status", "not_required"),
        "response_type": final_response.get("response_type"),
        "business_error_code": final_response.get("business_error_code"),
    }


def _persisted_assistant_response(
    message: AgentMessage,
    *,
    run: AgentRun | None,
) -> dict | None:
    if message.status != "completed":
        return None
    metadata = message.metadata_json or {}
    return {
        "content": message.content,
        "trace_id": run.trace_id if run is not None else None,
        "cited_evidence_ids": list(metadata.get("cited_evidence_ids", [])),
        "approval_status": metadata.get("approval_status", "not_required"),
        "response_type": metadata.get("response_type"),
        "business_error_code": metadata.get("business_error_code"),
    }


def _parse_approval_payload(record: ApprovalRecord) -> dict:
    try:
        payload = json.loads(record.payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ApprovalResumeIdentityError(
            "Approval payload is not valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise ApprovalResumeIdentityError(
            "Approval payload identity is invalid."
        )
    return payload


def _raise_persistence_unavailable(db: Session, exc: Exception) -> None:
    _rollback_safely(db)
    raise HTTPException(
        status_code=503,
        detail={"code": ApprovalPersistenceUnavailable.code},
    ) from exc


def _rollback_safely(db: Session) -> None:
    try:
        db.rollback()
    except SQLAlchemyError:
        pass
