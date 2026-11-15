from collections.abc import Iterable, Iterator

from ag_ui.core import BaseEvent, Interrupt, ResumeEntry, RunAgentInput, RunErrorEvent
from ag_ui.encoder import EventEncoder
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langgraph.store.base import BaseStore
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api import approvals as approval_api
from app.api import sessions as session_api
from app.approvals.repository import ApprovalRepository
from app.intent.catalog import BusinessIntentCatalog
from app.intent.dependencies import get_business_intent_catalog
from app.memory.dependencies import get_memory_store
from app.operations.rate_limit import require_rate_limit
from app.protocols.ag_ui import (
    adapt_approval_resume_to_ag_ui,
    adapt_session_sse_to_ag_ui,
)
from app.security.dependencies import require_permission
from app.security.principal import Principal
from app.storage.database import get_db
from app.storage.models import AgentRun, ApprovalRecord
from app.vision.context import VisionAttachmentContextResolver

router = APIRouter(prefix="/api", tags=["ag-ui"])


@router.post(
    "/ag-ui",
    dependencies=[
        Depends(require_rate_limit(session_api.AGENT_INVOCATION_RATE_LIMIT))
    ],
)
def run_ag_ui_agent(
    input_data: RunAgentInput,
    request: Request,
    business_intent_catalog: BusinessIntentCatalog = Depends(
        get_business_intent_catalog
    ),
    principal: Principal = Depends(require_permission("agent:invoke")),
    db: Session = Depends(get_db),
    attachment_resolver: VisionAttachmentContextResolver = Depends(
        session_api.get_attachment_context_resolver
    ),
    memory_store: BaseStore = Depends(get_memory_store),
) -> StreamingResponse:
    from app.security.stream_authorization import (
        StreamAccessError,
        StreamAuthorization,
        authorized_event_stream,
    )

    authorization = None
    def authorize_approval(record):
        nonlocal authorization
        authorization = StreamAuthorization(
            session_factory=sessionmaker(bind=db.get_bind(), autoflush=False),
            principal=principal, session_id=input_data.thread_id, run_id=record.run_id,
            resource_owner_id=record.requested_by,
            permissions=("agent:invoke", "approval:decide"),
        )
        try:
            authorization.check()
        except StreamAccessError as exc:
            raise HTTPException(status_code=exc.http_status,
                                detail={"code": exc.code, "message": exc.message}) from None

    if input_data.resume:
        events = _resume_ag_ui_agent(
            input_data=input_data,
            business_intent_catalog=business_intent_catalog,
            principal=principal,
            db=db,
            memory_store=memory_store,
            before_resume=authorize_approval,
        )
    else:
        authorization = StreamAuthorization(
            session_factory=sessionmaker(bind=db.get_bind(), autoflush=False),
            principal=principal, session_id=input_data.thread_id,
            permissions=("agent:invoke",), allow_new_session=True,
        )
        message = _latest_user_text(input_data)
        attachments = _attachment_ids(input_data)
        source = session_api._stream_agent_message(
            session_id=input_data.thread_id,
            request=session_api.SessionMessageRequest(
                message=message,
                attachments=attachments,
            ),
            principal=principal,
            db=db,
            attachment_resolver=attachment_resolver,
            memory_store=memory_store,
            business_intent_catalog=business_intent_catalog,
            request_id=input_data.run_id,
        )
        events = adapt_session_sse_to_ag_ui(
            source,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            messages=input_data.messages,
            interrupt_provider=lambda approval_ids: _approval_interrupts(
                db=db,
                tenant_id=principal.tenant_id,
                approval_ids=approval_ids,
            ),
        )
    encoder = EventEncoder(accept=request.headers.get("accept"))
    return StreamingResponse(
        session_api._closeable_async_stream(authorized_event_stream(
            _encode_events(events, encoder=encoder), authorization=authorization,
            error_encoder=lambda error: encoder.encode(RunErrorEvent(message=error.message, code=error.code)),
        )),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _resume_ag_ui_agent(
    *,
    input_data: RunAgentInput,
    business_intent_catalog: BusinessIntentCatalog,
    principal: Principal,
    db: Session,
    memory_store: BaseStore,
    before_resume=None,
) -> Iterable[BaseEvent]:
    _require_scope(principal, "approval:decide")
    entry = _single_resume_entry(input_data)
    record = _resolve_approval_record(
        db=db,
        tenant_id=principal.tenant_id,
        entry=entry,
    )
    _validate_resume_thread(
        db=db,
        tenant_id=principal.tenant_id,
        thread_id=input_data.thread_id,
        record=record,
    )
    if before_resume is not None:
        before_resume(record)
    result = approval_api.decide_approval(
        approval_id=record.approval_id,
        request=approval_api.ApprovalDecisionRequest(
            decision=_resume_decision(entry)
        ),
        business_intent_catalog=business_intent_catalog,
        principal=principal,
        db=db,
        memory_store=memory_store,
    )
    pending_ids = [
        pending.approval_id
        for pending in ApprovalRepository(db).list_pending(
            tenant_id=principal.tenant_id
        )
        if pending.run_id == record.run_id
    ]
    interrupts = _approval_interrupts(
        db=db,
        tenant_id=principal.tenant_id,
        approval_ids=pending_ids,
    )
    return adapt_approval_resume_to_ag_ui(
        result,
        thread_id=input_data.thread_id,
        run_id=input_data.run_id,
        interrupts=interrupts,
    )


def _single_resume_entry(input_data: RunAgentInput) -> ResumeEntry:
    entries = input_data.resume or []
    if len(entries) != 1:
        raise HTTPException(
            status_code=422,
            detail={"code": "ag_ui_single_resume_required"},
        )
    return entries[0]


def _resume_decision(entry: ResumeEntry) -> str:
    if entry.status == "cancelled":
        return "rejected"
    payload = entry.payload
    decision = payload.get("decision") if isinstance(payload, dict) else None
    if decision not in {"approved", "rejected"}:
        raise HTTPException(
            status_code=422,
            detail={"code": "ag_ui_resume_decision_invalid"},
        )
    return decision


def _resolve_approval_record(
    *,
    db: Session,
    tenant_id: str,
    entry: ResumeEntry,
) -> ApprovalRecord:
    repository = ApprovalRepository(db)
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    approval_id = metadata.get("approvalId")
    record = (
        repository.get(approval_id=approval_id, tenant_id=tenant_id)
        if isinstance(approval_id, str) and approval_id
        else repository.get_by_interrupt_id(
            interrupt_id=entry.interrupt_id,
            tenant_id=tenant_id,
        )
    )
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "approval_not_found"},
        )
    if record.interrupt_id != entry.interrupt_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "approval_resume_identity_mismatch"},
        )
    return record


def _validate_resume_thread(
    *,
    db: Session,
    tenant_id: str,
    thread_id: str,
    record: ApprovalRecord,
) -> None:
    run = db.scalar(
        select(AgentRun).where(
            AgentRun.tenant_id == tenant_id,
            AgentRun.run_id == record.run_id,
        )
    )
    if run is None or run.session_id != thread_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "approval_resume_identity_mismatch"},
        )


def _require_scope(principal: Principal, permission: str) -> None:
    if "*" not in principal.scopes and permission not in principal.scopes:
        raise HTTPException(
            status_code=403,
            detail={"code": "permission_denied", "permission": permission},
        )


def _latest_user_text(input_data: RunAgentInput) -> str:
    for message in reversed(input_data.messages):
        if message.role != "user":
            continue
        if isinstance(message.content, str):
            content = message.content.strip()
        else:
            content = "\n".join(
                str(item.text).strip()
                for item in message.content
                if getattr(item, "type", None) == "text"
                and str(getattr(item, "text", "")).strip()
            )
        if content:
            return content
    raise HTTPException(
        status_code=422,
        detail={"code": "ag_ui_user_message_required"},
    )


def _attachment_ids(input_data: RunAgentInput) -> list[str]:
    forwarded = input_data.forwarded_props
    if not isinstance(forwarded, dict):
        return []
    raw_ids = forwarded.get("attachmentIds", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(
            status_code=422,
            detail={"code": "ag_ui_attachment_ids_invalid"},
        )
    return [item for item in raw_ids if isinstance(item, str) and item]


def _approval_interrupts(
    *,
    db: Session,
    tenant_id: str,
    approval_ids: list[str],
) -> list[Interrupt]:
    repository = ApprovalRepository(db)
    interrupts = []
    for approval_id in approval_ids:
        record = repository.get(approval_id=approval_id, tenant_id=tenant_id)
        if record is None:
            continue
        interrupts.append(
            Interrupt(
                id=record.interrupt_id,
                reason="approval_required",
                message=f"{record.subject_name} 需要审批后才能继续。",
                response_schema={
                    "type": "object",
                    "properties": {
                        "decision": {
                            "type": "string",
                            "enum": ["approved", "rejected"],
                        }
                    },
                    "required": ["decision"],
                    "additionalProperties": False,
                },
                metadata={
                    "approvalId": record.approval_id,
                    "approvalType": record.approval_type,
                    "subjectName": record.subject_name,
                    "riskLevel": record.risk_level,
                },
            )
        )
    return interrupts


def _encode_events(
    events: Iterable[BaseEvent],
    *,
    encoder: EventEncoder,
) -> Iterator[str]:
    try:
        for event in events:
            yield encoder.encode(event)
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            close()
