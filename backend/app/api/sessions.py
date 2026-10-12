import json
import logging
from collections.abc import AsyncIterator, Generator, Iterator
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from langgraph.store.base import BaseStore
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import iterate_in_threadpool

from app.api.assets import get_asset_service
from app.approvals.repository import ApprovalRepository
from app.assets.service import AssetService
from app.conversations.repository import (
    ConversationConflictError,
    ConversationRepository,
    SessionBusyError,
)
from app.conversations.service import (
    BeginTurn,
    ConversationMetadata,
    ConversationService,
    TurnHandle,
)
from app.core.config import settings
from app.graph.checkpoints import build_graph_checkpointer
from app.graph.runtime import (
    AgentGraphExecutionError,
    AgentGraphStreamEvent,
    run_question_answer_graph,
    stream_question_answer_graph,
)
from app.graph.state import AgentState, build_checkpoint_thread_id
from app.harness.capture_policy import TraceCaptureOutcome
from app.harness.persistence import (
    TracePersistenceService,
    TraceRunIdentity,
    finalize_trace_best_effort,
)
from app.harness.trace_buffer import TraceBuffer
from app.intent.catalog import BusinessIntentCatalog
from app.intent.dependencies import get_business_intent_catalog
from app.llm.deepseek import LlmConfigurationError
from app.llm.factory import maybe_build_llm_client
from app.memory.dependencies import get_memory_store
from app.memory.synchronizer import sync_committed_run
from app.operations.rate_limit import RateLimitPolicy, require_rate_limit
from app.schemas.messages import Message
from app.security.dependencies import require_permission
from app.security.principal import Principal
from app.storage.database import get_db
from app.storage.repositories import persist_agent_run
from app.vision.context import VisionAttachmentContextResolver
from app.vision.repository import SqlAlchemyVisionAnalysisRepository

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
logger = logging.getLogger(__name__)

PRODUCT_PROGRESS_MESSAGES = {
    "planning": "正在规划分析范围",
    "executing_tools": "正在调用投研数据工具",
    "retrieving_knowledge": "正在检索补充资料",
    "analyzing_evidence": "正在核对并分析已获得的证据",
    "composing_answer": "正在生成最终投研报告",
}
PERSISTED_REASONING_LIMIT = 64_000
AGENT_INVOCATION_RATE_LIMIT = RateLimitPolicy(
    name="agent_invocation",
    requests_per_minute=settings.agent_rate_limit_per_minute,
)
CONVERSATION_HISTORY_LIMIT = 20


class ConversationPersistenceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class _AttachmentContextSnapshotResolver:
    tenant_id: str
    user_id: str
    session_id: str
    asset_ids: tuple[str, ...]
    contexts_json: tuple[str, ...]

    def resolve(
        self,
        asset_ids: list[str],
        *,
        principal: Principal,
        session_id: str,
    ) -> list[dict]:
        if (
            principal.tenant_id != self.tenant_id
            or principal.user_id != self.user_id
            or session_id != self.session_id
            or tuple(asset_ids) != self.asset_ids
        ):
            raise ValueError("attachment_snapshot_identity_mismatch")
        return [json.loads(context) for context in self.contexts_json]


@dataclass(frozen=True)
class _ExecutionSnapshot:
    history: tuple[Message, ...]
    attachment_resolver: _AttachmentContextSnapshotResolver


@dataclass
class _TraceLifecycle:
    failed: bool = True


def get_attachment_context_resolver(
    db: Session = Depends(get_db),
    asset_service: AssetService = Depends(get_asset_service),
) -> VisionAttachmentContextResolver:
    return VisionAttachmentContextResolver(
        asset_service=asset_service,
        analysis_repository=SqlAlchemyVisionAnalysisRepository(db),
    )


class SessionMessageRequest(BaseModel):
    message: str
    attachments: list[str] = Field(
        default_factory=list,
        max_length=settings.max_message_attachments,
    )


class AgentProtocolStreamRequest(BaseModel):
    input: dict | None = None
    context: dict | None = None
    command: dict | None = None
    config: dict | None = None


class SessionMessageResponse(BaseModel):
    content: str
    trace_id: str
    cited_evidence_ids: list[str]
    approval_status: str
    run_id: str
    approval_ids: list[str] = []
    response_type: str | None = None
    business_error_code: str | None = None
    message_id: str
    request_id: str


class SessionMessageItem(BaseModel):
    message_id: str
    request_id: str
    run_id: str
    role: str
    status: str
    content: str
    content_type: str
    sequence_no: int
    metadata: dict
    created_at: datetime
    completed_at: datetime | None


class SessionMessagePage(BaseModel):
    items: list[SessionMessageItem]
    next_before_sequence: int | None = None


class SessionItem(BaseModel):
    session_id: str
    status: str
    created_at: datetime
    updated_at: datetime


class SessionPage(BaseModel):
    items: list[SessionItem]


@router.get("", response_model=SessionPage)
def list_sessions(
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(require_permission("agent:read")),
    db: Session = Depends(get_db),
) -> SessionPage:
    records = ConversationRepository(db).list_owned_sessions(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        limit=limit,
    )
    return SessionPage(
        items=[
            SessionItem(
                session_id=record.session_id,
                status=record.status,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            for record in records
        ]
    )


@router.post(
    "/{session_id}/messages",
    response_model=SessionMessageResponse,
    dependencies=[Depends(require_rate_limit(AGENT_INVOCATION_RATE_LIMIT))],
)
def post_message(
    session_id: str,
    request: SessionMessageRequest,
    business_intent_catalog: BusinessIntentCatalog = Depends(get_business_intent_catalog),
    principal: Principal = Depends(require_permission("agent:invoke")),
    db: Session = Depends(get_db),
    attachment_resolver: VisionAttachmentContextResolver = Depends(get_attachment_context_resolver),
    memory_store: BaseStore = Depends(get_memory_store),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
) -> SessionMessageResponse:
    try:
        return _run_and_persist_agent_message(
            session_id=session_id,
            request=request,
            principal=principal,
            db=db,
            attachment_resolver=attachment_resolver,
            memory_store=memory_store,
            business_intent_catalog=business_intent_catalog,
            request_id=idempotency_key or f"req_{uuid4().hex}",
        )
    except AgentGraphExecutionError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={
                "code": exc.code,
                "node": exc.node_name,
                "message": exc.message,
            },
        ) from exc
    except LlmConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail=_llm_configuration_error_payload(exc),
        ) from exc
    except ConversationPersistenceUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "conversation_persistence_unavailable"},
        ) from exc
    except (ConversationConflictError, SessionBusyError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": str(exc)},
        ) from exc


@router.get("/{session_id}/messages", response_model=SessionMessagePage)
def list_messages(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    before_sequence: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(require_permission("agent:read")),
    db: Session = Depends(get_db),
) -> SessionMessagePage:
    repository = ConversationRepository(db)
    session_record = repository.get_owned_session(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        session_id=session_id,
    )
    if session_record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "session_not_found"},
        )
    records = repository.list_message_page(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        session_key=session_record.session_key,
        limit=limit + 1,
        before_sequence=before_sequence,
    )
    has_more = len(records) > limit
    if has_more:
        records = records[1:]
    return SessionMessagePage(
        items=[_message_item(record) for record in records],
        next_before_sequence=records[0].sequence_no if has_more else None,
    )


@router.post(
    "/{session_id}/messages/stream",
    dependencies=[Depends(require_rate_limit(AGENT_INVOCATION_RATE_LIMIT))],
)
def post_message_stream(
    session_id: str,
    request: SessionMessageRequest,
    business_intent_catalog: BusinessIntentCatalog = Depends(get_business_intent_catalog),
    principal: Principal = Depends(require_permission("agent:invoke")),
    db: Session = Depends(get_db),
    attachment_resolver: VisionAttachmentContextResolver = Depends(get_attachment_context_resolver),
    memory_store: BaseStore = Depends(get_memory_store),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
) -> StreamingResponse:
    return StreamingResponse(
        _closeable_async_stream(
            _stream_agent_message(
                session_id=session_id,
                request=request,
                principal=principal,
                db=db,
                attachment_resolver=attachment_resolver,
                memory_store=memory_store,
                business_intent_catalog=business_intent_catalog,
                request_id=idempotency_key or f"req_{uuid4().hex}",
            )
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/{session_id}/messages/protocol-stream",
    dependencies=[Depends(require_rate_limit(AGENT_INVOCATION_RATE_LIMIT))],
)
def post_message_protocol_stream(
    session_id: str,
    protocol_request: AgentProtocolStreamRequest,
    business_intent_catalog: BusinessIntentCatalog = Depends(get_business_intent_catalog),
    principal: Principal = Depends(require_permission("agent:invoke")),
    db: Session = Depends(get_db),
    attachment_resolver: VisionAttachmentContextResolver = Depends(get_attachment_context_resolver),
    memory_store: BaseStore = Depends(get_memory_store),
) -> StreamingResponse:
    message, attachments, request_id = _protocol_input(protocol_request)
    return StreamingResponse(
        _closeable_async_stream(
            _to_agent_protocol_stream(
                _stream_agent_message(
                    session_id=session_id,
                    request=SessionMessageRequest(
                        message=message,
                        attachments=attachments,
                    ),
                    principal=principal,
                    db=db,
                    attachment_resolver=attachment_resolver,
                    memory_store=memory_store,
                    business_intent_catalog=business_intent_catalog,
                    request_id=request_id,
                ),
            )
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _load_execution_snapshot(
    *,
    db: Session,
    principal: Principal,
    session_id: str,
    request: SessionMessageRequest,
    handle: TurnHandle,
    attachment_resolver: VisionAttachmentContextResolver,
) -> _ExecutionSnapshot:
    history_records = ConversationRepository(db).list_context_messages(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        session_key=handle.session_key,
        exclude_request_id=handle.request_id,
        limit=CONVERSATION_HISTORY_LIMIT,
    )
    history = tuple(
        Message(
            message_id=record.message_id,
            role=record.role,
            content=record.content,
            metadata={"request_id": record.request_id},
        )
        for record in history_records
    )
    attachment_contexts = (
        attachment_resolver.resolve(
            request.attachments,
            principal=principal,
            session_id=session_id,
        )
        if request.attachments
        else []
    )
    return _ExecutionSnapshot(
        history=history,
        attachment_resolver=_AttachmentContextSnapshotResolver(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_id=session_id,
            asset_ids=tuple(request.attachments),
            contexts_json=tuple(
                json.dumps(context, ensure_ascii=False, sort_keys=True)
                for context in attachment_contexts
            ),
        ),
    )


def _run_and_persist_agent_message(
    *,
    session_id: str,
    request: SessionMessageRequest,
    principal: Principal,
    db: Session,
    attachment_resolver: VisionAttachmentContextResolver,
    memory_store: BaseStore,
    business_intent_catalog: BusinessIntentCatalog,
    request_id: str,
) -> SessionMessageResponse:
    conversation_service = ConversationService(db)
    handle: TurnHandle | None = None
    state: AgentState | None = None
    trace_buffer: TraceBuffer | None = None
    try:
        handle = conversation_service.begin_turn(
            BeginTurn(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                session_id=session_id,
                request_id=request_id,
                user_content=request.message,
                metadata=ConversationMetadata(attachment_ids=tuple(request.attachments)),
            )
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise ConversationPersistenceUnavailable from exc

    if handle.replayed:
        try:
            return _replayed_message_response(
                db=db,
                tenant_id=principal.tenant_id,
                handle=handle,
            )
        except SQLAlchemyError as exc:
            db.rollback()
            raise ConversationPersistenceUnavailable from exc

    trace_buffer = TraceBuffer()
    try:
        snapshot = _load_execution_snapshot(
            db=db,
            principal=principal,
            session_id=session_id,
            request=request,
            handle=handle,
            attachment_resolver=attachment_resolver,
        )
        db.commit()

        checkpointer = build_graph_checkpointer()
        llm_client = maybe_build_llm_client()
        state = run_question_answer_graph(
            business_intent_catalog=business_intent_catalog,
            session_id=session_id,
            session_key=handle.session_key,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            user_message=request.message,
            request_id=handle.request_id,
            trace_id=handle.trace_id,
            user_message_id=handle.user_message_id,
            assistant_message_id=handle.assistant_message_id,
            history=list(snapshot.history),
            store=memory_store,
            trace_buffer=trace_buffer,
            checkpointer=checkpointer,
            llm_client=llm_client,
            permission_scopes=list(principal.scopes),
            attachment_ids=request.attachments,
            attachment_resolver=snapshot.attachment_resolver,
        )

        interrupt_payloads = _interrupt_payloads(state)
        _persist_agent_message_state(
            db=db,
            state=state,
            interrupt_payloads=interrupt_payloads,
            run_id=handle.run_id,
            update_run_status=False,
            commit=False,
        )
        if interrupt_payloads:
            conversation_service.interrupt_turn(
                tenant_id=principal.tenant_id,
                run_id=handle.run_id,
            )
        else:
            final_response = state["final_response"] or {}
            conversation_service.complete_turn(
                tenant_id=principal.tenant_id,
                run_id=handle.run_id,
                content=final_response.get("content", ""),
                metadata=_assistant_response_metadata(final_response),
            )
        db.commit()
        sync_committed_run(db=db, store=memory_store, run_id=handle.run_id)
    except SQLAlchemyError as exc:
        db.rollback()
        try:
            _fail_turn_after_error(
                db=db,
                conversation_service=conversation_service,
                principal=principal,
                handle=handle,
            )
        except SQLAlchemyError:
            pass
        _finalize_trace_best_effort(
            db=db,
            handle=handle,
            buffer=trace_buffer,
            state=state,
            principal=principal,
            failed=True,
        )
        raise ConversationPersistenceUnavailable from exc
    except Exception:
        db.rollback()
        try:
            _fail_turn_after_error(
                db=db,
                conversation_service=conversation_service,
                principal=principal,
                handle=handle,
            )
        except SQLAlchemyError as exc:
            raise ConversationPersistenceUnavailable from exc
        _finalize_trace_best_effort(
            db=db,
            handle=handle,
            buffer=trace_buffer,
            state=state,
            principal=principal,
            failed=True,
        )
        raise
    _finalize_trace_best_effort(
        db=db,
        handle=handle,
        buffer=trace_buffer,
        state=state,
        principal=principal,
        failed=False,
        approval_occurred=bool(interrupt_payloads),
    )
    return _session_message_response(state, handle=handle)


def _fail_turn_after_error(
    *,
    db: Session,
    conversation_service: ConversationService,
    principal: Principal,
    handle: TurnHandle,
) -> None:
    try:
        conversation_service.fail_turn(
            tenant_id=principal.tenant_id,
            run_id=handle.run_id,
            error_code="agent_execution_failed",
        )
        db.commit()
    except ConversationConflictError:
        db.rollback()
    except SQLAlchemyError:
        db.rollback()
        raise


def _stream_agent_message(
    *,
    session_id: str,
    request: SessionMessageRequest,
    principal: Principal,
    db: Session,
    attachment_resolver: VisionAttachmentContextResolver,
    memory_store: BaseStore,
    business_intent_catalog: BusinessIntentCatalog,
    request_id: str,
) -> Iterator[str]:
    conversation_service = ConversationService(db)
    handle: TurnHandle | None = None
    state: AgentState | None = None
    trace_buffer: TraceBuffer | None = None
    finalized = False
    trace_lifecycle = _TraceLifecycle()
    try:
        handle = conversation_service.begin_turn(
            BeginTurn(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                session_id=session_id,
                request_id=request_id,
                user_content=request.message,
                metadata=ConversationMetadata(attachment_ids=tuple(request.attachments)),
            )
        )
        db.commit()
        if handle.replayed:
            replayed = _replayed_message_response(
                db=db,
                tenant_id=principal.tenant_id,
                handle=handle,
            )
            db.commit()
            identity = _stream_identity(handle)
            yield _sse("status", {"stage": "accepted", **identity})
            yield _sse(
                "final",
                {
                    "content": replayed.content,
                    "trace_id": replayed.trace_id,
                    "cited_evidence_ids": replayed.cited_evidence_ids,
                    "approval_status": replayed.approval_status,
                    "approval_ids": replayed.approval_ids,
                    "response_type": replayed.response_type,
                    "business_error_code": replayed.business_error_code,
                    **identity,
                },
            )
            yield _sse("done", {"ok": True, "replayed": True, **identity})
            return

        trace_buffer = TraceBuffer()
        identity = _stream_identity(handle)
        yield _sse("status", {"stage": "accepted", **identity})
        snapshot = _load_execution_snapshot(
            db=db,
            principal=principal,
            session_id=session_id,
            request=request,
            handle=handle,
            attachment_resolver=attachment_resolver,
        )
        db.commit()
        yield _sse("progress", _product_progress_payload("planning"))
        if request.attachments:
            yield _sse(
                "status",
                {
                    "stage": "attachment_loading",
                    "attachment_count": len(request.attachments),
                },
            )

        llm_client = maybe_build_llm_client()
        emitted_event_count = 0
        emitted_progress_stages = {"planning"}
        answer_streamed = False
        research_process: list[dict] = []
        for stream_item in stream_question_answer_graph(
            business_intent_catalog=business_intent_catalog,
            session_id=session_id,
            session_key=handle.session_key,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            user_message=request.message,
            request_id=handle.request_id,
            trace_id=handle.trace_id,
            user_message_id=handle.user_message_id,
            assistant_message_id=handle.assistant_message_id,
            history=list(snapshot.history),
            store=memory_store,
            trace_buffer=trace_buffer,
            checkpointer=build_graph_checkpointer(),
            llm_client=llm_client,
            permission_scopes=list(principal.scopes),
            attachment_ids=request.attachments,
            attachment_resolver=snapshot.attachment_resolver,
        ):
            if isinstance(stream_item, AgentGraphStreamEvent):
                if stream_item.event == "custom":
                    public_event = stream_item.data
                    if not isinstance(public_event, dict):
                        continue
                    event_type = public_event.get("type")
                    if event_type == "progress":
                        _collect_research_process_event(research_process, public_event)
                        yield _sse("progress", public_event)
                    elif event_type == "answer_delta":
                        content = str(public_event.get("content") or "")
                        if content:
                            answer_streamed = True
                            yield _sse("answer_delta", {"content": content})
                    elif event_type == "thinking_delta":
                        content = str(public_event.get("content") or "")
                        if content:
                            _collect_research_reasoning_delta(
                                research_process,
                                public_event,
                            )
                            yield _sse(
                                "thinking_delta",
                                {
                                    "id": public_event.get("id"),
                                    "stage": public_event.get("stage"),
                                    "label": public_event.get("label"),
                                    "content": content,
                                },
                            )
                    continue
                chunk, metadata = stream_item.data
                node_name = str(metadata.get("langgraph_node") or "analysis")
                if node_name != "synthesizer":
                    continue
                content = _message_chunk_text(chunk)
                if content:
                    answer_streamed = True
                    yield _sse("answer_delta", {"content": content})
                continue
            state = stream_item
            events = trace_buffer.events
            for event in events[emitted_event_count:]:
                yield _sse("agent_event", _trace_event_payload(event))
            emitted_event_count = len(events)
            progress_stage = _product_progress_stage(state)
            if progress_stage and progress_stage not in emitted_progress_stages:
                emitted_progress_stages.add(progress_stage)
                yield _sse(
                    "progress",
                    _product_progress_payload(progress_stage),
                )
        if state is None:
            raise AgentGraphExecutionError(
                node_name="graph_runtime",
                message="Agent 图未返回任何状态。",
                code="agent_stream_empty",
                http_status=500,
            )
        interrupt_payloads = _interrupt_payloads(state)
        _persist_agent_message_state(
            db=db,
            state=state,
            interrupt_payloads=interrupt_payloads,
            run_id=handle.run_id,
            update_run_status=False,
            commit=False,
        )
        if interrupt_payloads:
            conversation_service.interrupt_turn(
                tenant_id=principal.tenant_id,
                run_id=handle.run_id,
            )
        db.commit()
        sync_committed_run(db=db, store=memory_store, run_id=handle.run_id)
        if interrupt_payloads:
            finalized = True
            trace_lifecycle.failed = False
        finalized = yield from _emit_stream_result(
            db=db,
            conversation_service=conversation_service,
            principal=principal,
            handle=handle,
            state=state,
            interrupt_payloads=interrupt_payloads,
            llm_client=llm_client,
            trace_buffer=trace_buffer,
            trace_lifecycle=trace_lifecycle,
            answer_streamed=answer_streamed,
            research_process=research_process,
        )
    except AgentGraphExecutionError as exc:
        finalized = _fail_stream_turn(
            db=db,
            conversation_service=conversation_service,
            principal=principal,
            handle=handle,
        )
        yield _sse(
            "error",
            {
                "code": exc.code,
                "node": exc.node_name,
                "message": exc.message,
                "http_status": exc.http_status,
                **_stream_identity(handle),
            },
        )
        yield _sse("done", {"ok": False, **_stream_identity(handle)})
        return
    except LlmConfigurationError as exc:
        finalized = _fail_stream_turn(
            db=db,
            conversation_service=conversation_service,
            principal=principal,
            handle=handle,
        )
        yield _sse(
            "error",
            {**_llm_configuration_error_payload(exc), **_stream_identity(handle)},
        )
        yield _sse("done", {"ok": False, **_stream_identity(handle)})
        return
    except (ConversationConflictError, SessionBusyError) as exc:
        finalized = _fail_stream_turn(
            db=db,
            conversation_service=conversation_service,
            principal=principal,
            handle=handle,
        )
        yield _sse(
            "error",
            {
                "code": str(exc),
                "node": "conversation",
                "message": "请求正在执行或会话忙。",
                "http_status": 409,
                **_stream_identity(handle),
            },
        )
        yield _sse("done", {"ok": False, **_stream_identity(handle)})
        return
    except Exception:
        logger.exception("agent_stream_unhandled_error")
        finalized = _fail_stream_turn(
            db=db,
            conversation_service=conversation_service,
            principal=principal,
            handle=handle,
        )
        yield _sse(
            "error",
            {
                "code": "agent_stream_internal_error",
                "node": "agent_stream",
                "message": "Agent 流式执行失败，请查看后端日志定位具体原因。",
                "http_status": 500,
                **_stream_identity(handle),
            },
        )
        yield _sse("done", {"ok": False, **_stream_identity(handle)})
        return
    finally:
        if handle is not None and not handle.replayed and not finalized:
            _fail_stream_turn(
                db=db,
                conversation_service=conversation_service,
                principal=principal,
                handle=handle,
            )
        _finalize_trace_best_effort(
            db=db,
            handle=handle,
            buffer=trace_buffer,
            state=state,
            principal=principal,
            failed=trace_lifecycle.failed,
            approval_occurred=bool(state and _interrupt_payloads(state)),
        )


def _emit_stream_result(
    *,
    db: Session,
    conversation_service: ConversationService,
    principal: Principal,
    handle: TurnHandle,
    state: AgentState,
    interrupt_payloads: list[dict],
    llm_client,
    trace_buffer: TraceBuffer,
    trace_lifecycle: _TraceLifecycle,
    answer_streamed: bool = False,
    research_process: list[dict] | None = None,
) -> Generator[str, None, bool]:
    identity = _stream_identity(handle)
    if state["image_analyses"]:
        yield _sse(
            "status",
            {
                "stage": "image_analysis_loaded",
                "attachment_count": len(state["image_analyses"]),
                "partial_count": sum(
                    analysis["status"] == "partial" for analysis in state["image_analyses"]
                ),
            },
        )

    if interrupt_payloads:
        yield _sse(
            "approval_required",
            {
                "trace_id": state["trace_id"],
                "approval_ids": [payload["approval_id"] for payload in interrupt_payloads],
                **identity,
            },
        )
        yield _sse(
            "done",
            {"ok": True, "pending_approval": True, **identity},
        )
        return True

    final_response = state["final_response"]
    yield _sse(
        "status",
        {
            "stage": "graph_completed",
            "trace_id": final_response["trace_id"],
        },
    )
    yield _sse(
        "trace_events",
        {
            "trace_id": final_response["trace_id"],
            "events": [_trace_event_payload(event) for event in trace_buffer.events],
        },
    )
    conversation_service.mark_streaming(
        tenant_id=principal.tenant_id,
        run_id=handle.run_id,
    )
    db.commit()
    yield _sse("status", {"stage": "answering", "trace_id": final_response["trace_id"]})
    if not answer_streamed:
        for chunk in _chunk_text(final_response["content"]):
            yield _sse("answer_delta", {"content": chunk})
    conversation_service.complete_turn(
        tenant_id=principal.tenant_id,
        run_id=handle.run_id,
        content=final_response["content"],
        metadata=_assistant_response_metadata(
            final_response,
            research_process=research_process,
        ),
    )
    db.commit()
    trace_lifecycle.failed = False
    yield _sse("final", {**final_response, **identity})
    yield _sse("done", {"ok": True, **identity})
    return True


def _stream_identity(handle: TurnHandle | None) -> dict[str, str]:
    if handle is None:
        return {}
    return {
        "run_id": handle.run_id,
        "request_id": handle.request_id,
        "message_id": handle.assistant_message_id,
    }


def _fail_stream_turn(
    *,
    db: Session,
    conversation_service: ConversationService,
    principal: Principal,
    handle: TurnHandle | None,
) -> bool:
    if handle is None or handle.replayed:
        db.rollback()
        return True
    db.rollback()
    try:
        _fail_turn_after_error(
            db=db,
            conversation_service=conversation_service,
            principal=principal,
            handle=handle,
        )
    except SQLAlchemyError:
        logger.exception("agent_stream_failure_finalization_error")
    return True


def _finalize_trace_best_effort(
    *,
    db: Session,
    handle: TurnHandle | None,
    buffer: TraceBuffer | None,
    state: AgentState | None,
    principal: Principal,
    failed: bool,
    approval_occurred: bool = False,
) -> None:
    if handle is None or handle.replayed or buffer is None:
        return
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
            run_id=handle.run_id,
            trace_id=handle.trace_id,
            session_key=handle.session_key,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
        ),
        buffer=buffer,
        outcome=TraceCaptureOutcome(
            failed=failed,
            risk_level=intent.get("risk_level"),
            approval_occurred=approval_occurred,
            verifier_status=verification.get("verdict"),
        ),
        service=TracePersistenceService(session_factory=session_factory),
    )


def _persist_agent_message_state(
    *,
    db: Session,
    state: AgentState,
    interrupt_payloads: list[dict],
    run_id: str | None = None,
    update_run_status: bool = True,
    commit: bool = True,
) -> None:
    for payload in interrupt_payloads:
        ApprovalRepository(db).create_pending_from_interrupt(
            {
                **payload,
                "run_id": run_id or payload["run_id"],
            }
        )
    persist_agent_run(
        session=db,
        state=state,
        status="interrupted" if interrupt_payloads else "completed",
        run_id=run_id,
        update_run_status=update_run_status,
        commit=commit,
    )


def _sse(event: str, data: dict | list) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _closeable_async_stream(frames: Iterator[str]) -> AsyncIterator[str]:
    try:
        async for frame in iterate_in_threadpool(frames):
            yield frame
    finally:
        close = getattr(frames, "close", None)
        if callable(close):
            close()


def _protocol_input(
    request: AgentProtocolStreamRequest,
) -> tuple[str, list[str], str]:
    if request.command is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "protocol_command_not_supported"},
        )
    payload = request.input or {}
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(
            status_code=422,
            detail={"code": "protocol_messages_required"},
        )
    message = next(
        (
            str(item.get("content") or "").strip()
            for item in reversed(messages)
            if isinstance(item, dict)
            and str(item.get("type") or item.get("role") or "").lower() in {"human", "user"}
            and str(item.get("content") or "").strip()
        ),
        "",
    )
    if not message:
        raise HTTPException(
            status_code=422,
            detail={"code": "protocol_user_message_required"},
        )
    raw_attachments = payload.get("attachment_ids", [])
    attachments = (
        [str(item) for item in raw_attachments] if isinstance(raw_attachments, list) else []
    )
    raw_request_id = payload.get("request_id")
    request_id = (
        str(raw_request_id)
        if isinstance(raw_request_id, str) and raw_request_id.strip()
        else f"req_{uuid4().hex}"
    )
    return message, attachments, request_id


def _to_agent_protocol_stream(frames: Iterator[str]) -> Iterator[str]:
    assistant_message_id = f"assistant_{uuid4().hex}"
    answer_emitted = False
    try:
        for frame in frames:
            event, data = _parse_sse_frame(frame)
            if event == "status" and isinstance(data.get("message_id"), str):
                assistant_message_id = data["message_id"]
                continue
            if event == "answer_delta":
                content = str(data.get("content") or "")
                if content:
                    answer_emitted = True
                    yield _sse(
                        "messages",
                        [
                            {
                                "type": "ai",
                                "id": assistant_message_id,
                                "content": content,
                            },
                            {"langgraph_node": "synthesizer"},
                        ],
                    )
                continue
            if event in {"progress", "thinking_delta"}:
                yield _sse("custom", {"type": event, **data})
                continue
            if event == "approval_required":
                yield _sse("custom", {"type": "approval_required", **data})
                continue
            if event == "final":
                content = str(data.get("content") or "")
                if content and not answer_emitted:
                    yield _sse(
                        "messages",
                        [
                            {
                                "type": "ai",
                                "id": assistant_message_id,
                                "content": content,
                            },
                            {"langgraph_node": "synthesizer"},
                        ],
                    )
                yield _sse("custom", {"type": "final", "response": data})
                continue
            if event == "error":
                yield _sse("error", data)
    finally:
        close = getattr(frames, "close", None)
        if callable(close):
            close()


def _parse_sse_frame(frame: str) -> tuple[str, dict]:
    event = "message"
    data: dict = {}
    for line in frame.splitlines():
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            decoded = json.loads(line.removeprefix("data:").strip())
            data = decoded if isinstance(decoded, dict) else {"value": decoded}
    return event, data


def _message_chunk_text(chunk) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _product_progress_payload(stage: str) -> dict[str, str]:
    return {"stage": stage, "message": PRODUCT_PROGRESS_MESSAGES[stage]}


def _product_progress_stage(state: AgentState) -> str | None:
    if state.get("draft_answer") is not None or state.get("final_response") is not None:
        return None

    plan = state.get("plan") or {}
    tasks = [item for item in plan.get("tasks", []) if isinstance(item, dict)]
    if not tasks:
        return None
    results = {
        str(item.get("task_id")): item
        for item in state.get("task_results", [])
        if isinstance(item, dict) and item.get("task_id")
    }
    if all(
        (result := results.get(str(task.get("id")))) is not None
        and result.get("status") == "success"
        for task in tasks
    ):
        return "composing_answer"

    active_task = state.get("active_task") or {}
    tool_name = active_task.get("tool_name")
    if tool_name == "rag_search":
        return "retrieving_knowledge"
    if isinstance(tool_name, str) and tool_name:
        return "executing_tools"
    if active_task or results:
        return "analyzing_evidence"
    return None


def _llm_configuration_error_payload(exc: LlmConfigurationError) -> dict:
    return {
        "code": "llm_configuration_error",
        "node": "llm_provider",
        "message": ("LLM Provider 配置不完整，请设置 BS_AGENT_DEEPSEEK_API_KEY 后重启本地服务。"),
        "http_status": 503,
        "details": {"provider": settings.llm_provider, "reason": str(exc)},
    }


def _chunk_text(content: str, *, chunk_size: int = 12) -> Iterator[str]:
    for index in range(0, len(content), chunk_size):
        yield content[index : index + chunk_size]


def _trace_event_payload(event) -> dict:
    return {
        "event_id": event.event_id,
        "trace_id": event.trace_id,
        "node_name": event.node_name,
        "status": event.status,
        "latency_ms": event.latency_ms,
        "input_ref": event.input_ref,
        "output_ref": event.output_ref,
        "error": event.error,
        "metadata": event.metadata,
    }


def _interrupt_payloads(state: AgentState) -> list[dict]:
    raw_interrupts = state.get("__interrupt__", [])
    if not raw_interrupts:
        return []
    config = {
        "configurable": {
            "thread_id": build_checkpoint_thread_id(
                tenant_id=state["tenant_id"],
                session_key=state["session_key"],
                request_id=state["request_id"],
            ),
            "checkpoint_ns": "",
        }
    }
    checkpoint_tuple = build_graph_checkpointer().get_tuple(config)
    if checkpoint_tuple is None:
        raise AgentGraphExecutionError(
            node_name="approval_projection",
            message="未找到审批 Interrupt 对应的 checkpoint。",
            code="approval_checkpoint_missing",
            http_status=409,
        )
    checkpoint_id = checkpoint_tuple.config["configurable"]["checkpoint_id"]
    payloads = []
    for item in raw_interrupts:
        payload = getattr(item, "value", item)
        interrupt_id = getattr(item, "id", None)
        if not isinstance(payload, dict) or not interrupt_id:
            raise AgentGraphExecutionError(
                node_name="approval_projection",
                message="审批 Interrupt 格式无效。",
                code="approval_interrupt_invalid",
                http_status=409,
            )
        payloads.append(
            {
                **payload,
                "checkpoint_thread_id": config["configurable"]["thread_id"],
                "checkpoint_namespace": "",
                "checkpoint_id": checkpoint_id,
                "interrupt_id": interrupt_id,
            }
        )
    return payloads


def _session_message_response(
    state: AgentState,
    *,
    handle: TurnHandle | None = None,
) -> SessionMessageResponse:
    message_id = handle.assistant_message_id if handle else state["assistant_message_id"]
    request_id = handle.request_id if handle else state["request_id"]
    run_id = handle.run_id if handle else state["request_id"]
    interrupt_payloads = _interrupt_payloads(state)
    if interrupt_payloads:
        return SessionMessageResponse(
            content="",
            trace_id=state["trace_id"],
            cited_evidence_ids=[],
            approval_status="pending",
            run_id=run_id,
            approval_ids=[payload["approval_id"] for payload in interrupt_payloads],
            message_id=message_id,
            request_id=request_id,
        )
    final_response = state["final_response"] or {}
    return SessionMessageResponse(
        content=final_response.get("content", ""),
        trace_id=final_response.get("trace_id", state["trace_id"]),
        cited_evidence_ids=final_response.get("cited_evidence_ids", []),
        approval_status=final_response.get("approval_status", "not_required"),
        run_id=run_id,
        approval_ids=[],
        response_type=final_response.get("response_type"),
        business_error_code=final_response.get("business_error_code"),
        message_id=message_id,
        request_id=request_id,
    )


def _assistant_response_metadata(
    final_response: dict,
    *,
    research_process: list[dict] | None = None,
) -> dict:
    metadata = {
        "cited_evidence_ids": list(final_response.get("cited_evidence_ids", [])),
        "approval_status": final_response.get("approval_status", "not_required"),
        "response_type": final_response.get("response_type"),
        "business_error_code": final_response.get("business_error_code"),
    }
    if research_process:
        metadata["research_process"] = research_process[:32]
    return metadata


def _collect_research_process_event(events: list[dict], event: dict) -> None:
    kind = event.get("kind")
    if kind not in {"activity", "summary"}:
        return
    event_id = str(event.get("id") or "").strip()[:128]
    message = str(event.get("message") or "").strip()[:500]
    if not event_id or not message:
        return
    normalized = {
        "id": event_id,
        "kind": kind,
        "label": str(event.get("label") or "研究过程").strip()[:80],
        "message": message,
        "status": (
            event.get("status")
            if event.get("status") in {"started", "completed", "failed"}
            else "started"
        ),
    }
    activity_type = event.get("activity_type")
    if activity_type in {"web_search", "market_data", "evidence"}:
        normalized["activity_type"] = activity_type
        for key, limit in (
            ("group_id", 128),
            ("task_id", 128),
            ("task_label", 500),
            ("query", 500),
        ):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                normalized[key] = value.strip()[:limit]
        result_count = event.get("result_count")
        if isinstance(result_count, int) and result_count >= 0:
            normalized["result_count"] = result_count
        result_urls = event.get("result_urls")
        if isinstance(result_urls, list):
            normalized["result_urls"] = [
                url[:1000]
                for url in result_urls[:50]
                if isinstance(url, str) and url.startswith(("http://", "https://"))
            ]
    existing_index = next(
        (index for index, item in enumerate(events) if item.get("id") == event_id),
        None,
    )
    if existing_index is not None:
        normalized["sequence"] = events[existing_index]["sequence"]
        events[existing_index] = normalized
        return
    if len(events) >= 32:
        return
    normalized["sequence"] = len(events)
    events.append(normalized)


def _collect_research_reasoning_delta(events: list[dict], event: dict) -> None:
    event_id = str(event.get("id") or event.get("stage") or "reasoning").strip()[:128]
    content = event.get("content")
    if not event_id or not isinstance(content, str) or not content:
        return
    existing_index = next(
        (
            index
            for index, item in enumerate(events)
            if item.get("kind") == "reasoning" and item.get("id") == event_id
        ),
        None,
    )
    if existing_index is not None:
        existing = events[existing_index]
        existing["content"] = (f"{existing.get('content', '')}{content}")[
            :PERSISTED_REASONING_LIMIT
        ]
        return
    if len(events) >= 32:
        return
    events.append(
        {
            "id": event_id,
            "kind": "reasoning",
            "label": str(event.get("label") or "思考").strip()[:80],
            "content": content[:PERSISTED_REASONING_LIMIT],
            "sequence": len(events),
        }
    )


def _replayed_message_response(
    *,
    db: Session,
    tenant_id: str,
    handle: TurnHandle,
) -> SessionMessageResponse:
    turn = ConversationRepository(db).get_turn_by_request(
        tenant_id=tenant_id,
        request_id=handle.request_id,
    )
    if turn is None:
        raise ConversationConflictError("turn_replay_missing")
    assistant = turn[1]
    if assistant.status != "completed":
        raise ConversationConflictError("request_in_progress")
    metadata = assistant.metadata_json or {}
    return SessionMessageResponse(
        content=assistant.content,
        trace_id=handle.trace_id,
        cited_evidence_ids=list(metadata.get("cited_evidence_ids", [])),
        approval_status=metadata.get("approval_status", "not_required"),
        run_id=handle.run_id,
        approval_ids=[],
        response_type=metadata.get("response_type"),
        business_error_code=metadata.get("business_error_code"),
        message_id=handle.assistant_message_id,
        request_id=handle.request_id,
    )


def _message_item(record) -> SessionMessageItem:
    return SessionMessageItem(
        message_id=record.message_id,
        request_id=record.request_id,
        run_id=record.run_id,
        role=record.role,
        status=record.status,
        content=record.content,
        content_type=record.content_type,
        sequence_no=record.sequence_no,
        metadata=record.metadata_json or {},
        created_at=record.created_at,
        completed_at=record.completed_at,
    )
