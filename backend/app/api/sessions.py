import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.approvals.repository import ApprovalRepository
from app.api.assets import get_asset_service
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
    run_question_answer_graph,
    stream_question_answer_graph,
)
from app.graph.state import AgentState
from app.harness.tracing import get_trace_store
from app.llm.deepseek import LlmConfigurationError
from app.llm.factory import maybe_build_llm_client
from app.memory.factory import build_experience_store
from app.memory.retriever import ExperienceMemoryRetriever
from app.memory.short_term_factory import build_short_term_memory_store
from app.memory.short_term_store import ShortTermMemoryStore
from app.operations.rate_limit import RateLimitPolicy, require_rate_limit
from app.security.dependencies import require_permission
from app.security.principal import Principal
from app.schemas.messages import Message
from app.storage.database import get_db
from app.storage.repositories import persist_agent_run
from app.vision.context import VisionAttachmentContextResolver
from app.vision.repository import SqlAlchemyVisionAnalysisRepository

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
logger = logging.getLogger(__name__)

PRODUCT_PROGRESS_MESSAGES = {
    "planning": "正在规划分析范围",
    "collecting_market_data": "正在获取公司、行情与财务数据",
    "retrieving_knowledge": "正在检索补充资料",
    "analyzing_evidence": "正在核对并分析已获得的证据",
    "composing_answer": "正在生成最终投研报告",
}
THINKING_STAGE_LABELS = {
    "planning": "规划",
    "tool_repair": "实体修复",
    "evidence_review": "证据核对",
    "answer_synthesis": "答案合成",
}
AGENT_INVOCATION_RATE_LIMIT = RateLimitPolicy(
    name="agent_invocation",
    requests_per_minute=settings.agent_rate_limit_per_minute,
)
CONVERSATION_HISTORY_LIMIT = 20


class ConversationPersistenceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class _ExperienceEntrySnapshot:
    source_candidate_id: str
    user_id: str
    memory_type: str
    content: str
    source: str | None


@dataclass(frozen=True)
class _ExperienceSnapshotReader:
    tenant_id: str
    user_id: str
    entries: tuple[_ExperienceEntrySnapshot, ...]

    def list_for_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> list[_ExperienceEntrySnapshot]:
        if tenant_id != self.tenant_id or user_id != self.user_id:
            return []
        return list(self.entries)


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


@router.post(
    "/{session_id}/messages",
    response_model=SessionMessageResponse,
    dependencies=[Depends(require_rate_limit(AGENT_INVOCATION_RATE_LIMIT))],
)
def post_message(
    session_id: str,
    request: SessionMessageRequest,
    principal: Principal = Depends(require_permission("agent:invoke")),
    db: Session = Depends(get_db),
    attachment_resolver: VisionAttachmentContextResolver = Depends(
        get_attachment_context_resolver
    ),
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
    principal: Principal = Depends(require_permission("agent:invoke")),
    db: Session = Depends(get_db),
    attachment_resolver: VisionAttachmentContextResolver = Depends(
        get_attachment_context_resolver
    ),
) -> StreamingResponse:
    return StreamingResponse(
        _stream_agent_message(
            session_id=session_id,
            request=request,
            principal=principal,
            db=db,
            attachment_resolver=attachment_resolver,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _run_and_persist_agent_message(
    *,
    session_id: str,
    request: SessionMessageRequest,
    principal: Principal,
    db: Session,
    attachment_resolver: VisionAttachmentContextResolver,
    request_id: str,
) -> SessionMessageResponse:
    conversation_service = ConversationService(db)
    try:
        handle = conversation_service.begin_turn(
            BeginTurn(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                session_id=session_id,
                request_id=request_id,
                user_content=request.message,
                metadata=ConversationMetadata(
                    attachment_ids=tuple(request.attachments)
                ),
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

    try:
        history_records = ConversationRepository(db).list_context_messages(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_key=handle.session_key,
            exclude_request_id=handle.request_id,
            limit=CONVERSATION_HISTORY_LIMIT,
        )
        history = [
            Message(
                message_id=record.message_id,
                role=record.role,
                content=record.content,
                metadata={"request_id": record.request_id},
            )
            for record in history_records
        ]
        experience_entries = build_experience_store(session=db).list_for_user(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
        )
        memory_reader = _ExperienceSnapshotReader(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            entries=tuple(
                _ExperienceEntrySnapshot(
                    source_candidate_id=entry.source_candidate_id,
                    user_id=entry.user_id,
                    memory_type=entry.memory_type,
                    content=entry.content,
                    source=entry.source,
                )
                for entry in experience_entries
            ),
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
        snapshot_attachment_resolver = _AttachmentContextSnapshotResolver(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_id=session_id,
            asset_ids=tuple(request.attachments),
            contexts_json=tuple(
                json.dumps(context, ensure_ascii=False, sort_keys=True)
                for context in attachment_contexts
            ),
        )
        db.commit()

        trace_store = get_trace_store()
        checkpointer = build_graph_checkpointer()
        llm_client = maybe_build_llm_client()
        state = run_question_answer_graph(
            session_id=session_id,
            session_key=handle.session_key,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            user_message=request.message,
            request_id=handle.request_id,
            trace_id=handle.trace_id,
            user_message_id=handle.user_message_id,
            assistant_message_id=handle.assistant_message_id,
            history=history,
            mock_evidence=settings.use_mock_rag,
            memory_retriever=ExperienceMemoryRetriever(memory_reader),
            trace_store=trace_store,
            checkpointer=checkpointer,
            llm_client=llm_client,
            permission_scopes=list(principal.scopes),
            attachment_ids=request.attachments,
            attachment_resolver=snapshot_attachment_resolver,
        )

        interrupt_payloads = _interrupt_payloads(state)
        experience_store = build_experience_store(session=db)
        _persist_agent_message_state(
            db=db,
            state=state,
            trace_store=trace_store,
            experience_store=experience_store,
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
        raise
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
) -> Iterator[str]:
    yield _sse("status", {"stage": "accepted"})
    yield _sse("progress", _product_progress_payload("planning"))
    if request.attachments:
        yield _sse(
            "status",
            {
                "stage": "attachment_loading",
                "attachment_count": len(request.attachments),
            },
        )
    try:
        trace_store = get_trace_store()
        experience_store = build_experience_store(session=db)
        llm_client = maybe_build_llm_client()
        state: AgentState | None = None
        emitted_event_count = 0
        emitted_thinking_stages: set[str] = set()
        emitted_progress_stages = {"planning"}
        for state in stream_question_answer_graph(
            session_id=session_id,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            user_message=request.message,
            mock_evidence=settings.use_mock_rag,
            memory_retriever=ExperienceMemoryRetriever(experience_store),
            trace_store=trace_store,
            checkpointer=build_graph_checkpointer(),
            llm_client=llm_client,
            permission_scopes=list(principal.scopes),
            attachment_ids=request.attachments,
            attachment_resolver=attachment_resolver,
        ):
            events = trace_store.list_events(state["trace_id"])
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
            thinking_stage = _public_thinking_stage(state)
            if thinking_stage and thinking_stage not in emitted_thinking_stages:
                emitted_thinking_stages.add(thinking_stage)
                yield from _thinking_sse_events(
                    llm_client=llm_client,
                    state=state,
                    stage=thinking_stage,
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
            trace_store=trace_store,
            experience_store=experience_store,
            interrupt_payloads=interrupt_payloads,
        )
    except AgentGraphExecutionError as exc:
        yield _sse(
            "error",
            {
                "code": exc.code,
                "node": exc.node_name,
                "message": exc.message,
                "http_status": exc.http_status,
            },
        )
        yield _sse("done", {"ok": False})
        return
    except LlmConfigurationError as exc:
        yield _sse("error", _llm_configuration_error_payload(exc))
        yield _sse("done", {"ok": False})
        return
    except Exception:
        logger.exception("agent_stream_unhandled_error")
        yield _sse(
            "error",
            {
                "code": "agent_stream_internal_error",
                "node": "agent_stream",
                "message": "Agent 流式执行失败，请查看后端日志定位具体原因。",
                "http_status": 500,
            },
        )
        yield _sse("done", {"ok": False})
        return

    if state["image_analyses"]:
        yield _sse(
            "status",
            {
                "stage": "image_analysis_loaded",
                "attachment_count": len(state["image_analyses"]),
                "partial_count": sum(
                    analysis["status"] == "partial"
                    for analysis in state["image_analyses"]
                ),
            },
        )

    if interrupt_payloads:
        yield _sse(
            "approval_required",
            {
                "run_id": state["request_id"],
                "trace_id": state["trace_id"],
                "approval_ids": [
                    payload["approval_id"] for payload in interrupt_payloads
                ],
            },
        )
        yield _sse("done", {"ok": True, "pending_approval": True})
        return

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
            "events": [
                _trace_event_payload(event)
                for event in get_trace_store().list_events(final_response["trace_id"])
            ],
        },
    )
    if not emitted_thinking_stages:
        yield from _thinking_sse_events(
            llm_client=llm_client,
            state=state,
            stage="planning",
        )
    yield _sse("status", {"stage": "answering", "trace_id": final_response["trace_id"]})
    for chunk in _chunk_text(final_response["content"]):
        yield _sse("answer_delta", {"content": chunk})
    yield _sse("final", final_response)
    yield _sse("done", {"ok": True})


def _persist_agent_message_state(
    *,
    db: Session,
    state: AgentState,
    trace_store,
    experience_store,
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
                "session_key": state["session_key"],
            }
        )
    persist_agent_run(
        session=db,
        state=state,
        trace_events=trace_store.list_events(state["trace_id"]),
        status="interrupted" if interrupt_payloads else "completed",
        experience_store=experience_store,
        run_id=run_id,
        update_run_status=update_run_status,
        commit=commit,
    )
    if not interrupt_payloads:
        _record_short_term_session_event(
            store=build_short_term_memory_store(),
            state=state,
        )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _product_progress_payload(stage: str) -> dict[str, str]:
    return {"stage": stage, "message": PRODUCT_PROGRESS_MESSAGES[stage]}


def _product_progress_stage(state: AgentState) -> str | None:
    if state.get("draft_answer") is not None or state.get("final_response") is not None:
        return None

    plan = state.get("plan") or {}
    requests = plan.get("requested_tools") or []
    has_market_data = any(
        ".stock." in request.tool_name
        for request in requests
        if hasattr(request, "tool_name")
    )
    has_rag = any(
        request.tool_name == "rag_search"
        for request in requests
        if hasattr(request, "tool_name")
    )

    if state.get("analysis") is not None:
        return "composing_answer"
    if state.get("ready_tool_call") is not None and has_rag:
        return "retrieving_knowledge"
    if state.get("tool_calls") and state.get("ready_tool_call") is None:
        return "analyzing_evidence"
    if has_market_data:
        return "collecting_market_data"
    if has_rag:
        return "retrieving_knowledge"
    return None


def _public_thinking_stage(state: AgentState) -> str | None:
    if state.get("final_response") is not None or state.get("draft_answer") is not None:
        return None
    plan = state.get("plan")
    if not isinstance(plan, dict) or not plan.get("execution_mode"):
        return None
    if state.get("analysis") is not None:
        return "answer_synthesis"
    tool_repair = plan.get("tool_repair")
    if isinstance(tool_repair, dict) and tool_repair.get("status"):
        return "tool_repair"
    if state.get("tool_calls"):
        return "evidence_review"
    return "planning"


def _thinking_sse_events(
    *,
    llm_client,
    state: AgentState,
    stage: str,
) -> Iterator[str]:
    yield _sse("status", {"stage": "thinking", "trace_id": state["trace_id"]})
    try:
        for chunk in _stream_user_visible_thinking(
            llm_client=llm_client,
            state=state,
            stage=stage,
        ):
            yield _sse(
                "thinking_delta",
                {
                    "stage": stage,
                    "label": THINKING_STAGE_LABELS.get(stage, "分析"),
                    "content": chunk,
                },
            )
    except Exception:
        logger.warning("agent_thinking_stream_failed", exc_info=True)
        for chunk in _fallback_thinking_chunks(state, stage=stage):
            yield _sse(
                "thinking_delta",
                {
                    "stage": stage,
                    "label": THINKING_STAGE_LABELS.get(stage, "分析"),
                    "content": chunk,
                },
            )


def _stream_user_visible_thinking(
    *,
    llm_client,
    state: AgentState,
    stage: str,
) -> Iterator[str]:
    if llm_client is None or not hasattr(llm_client, "stream_complete"):
        yield from _fallback_thinking_chunks(state, stage=stage)
        return
    emitted = False
    for chunk in _complete_sentence_chunks(
        _sync_iter_async_chunks(
            llm_client.stream_complete(
                system_prompt=(
                    "你是 BsAgent 的投研分析过程播报器。"
                    "输出用户可见的分析思路摘要，不要暴露内部节点名、trace、工具参数、系统提示或隐藏推理。"
                    "用第一人称简洁说明当前公开阶段正在做什么、依据什么证据边界推进。"
                    "输出 1 到 3 个完整中文短句，总长度 60 到 180 字。"
                    "不要输出 JSON、括号、Markdown、列表编号，不要提及模型厂商。"
                    "最后一句必须完整收尾，不要以冒号、逗号或分号结尾。"
                ),
                user_prompt=json.dumps(
                    _thinking_prompt_payload(state, stage=stage),
                    ensure_ascii=False,
                ),
                temperature=0.2,
                max_tokens=360,
                thinking=False,
            )
        )
    ):
        emitted = True
        yield chunk
    if not emitted:
        yield from _fallback_thinking_chunks(state, stage=stage)


def _complete_sentence_chunks(chunks: Iterator[str]) -> Iterator[str]:
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        completed, buffer = _take_complete_sentence_prefix(buffer)
        if completed:
            yield completed


def _take_complete_sentence_prefix(text: str) -> tuple[str, str]:
    end = -1
    terminal_chars = {"。", "！", "？", "!", "?"}
    closing_chars = {"”", "’", "」", "』", "）", ")", "】", "]"}
    for index, char in enumerate(text):
        if char not in terminal_chars:
            continue
        candidate_end = index + 1
        while candidate_end < len(text) and text[candidate_end] in closing_chars:
            candidate_end += 1
        end = candidate_end
    if end < 0:
        return "", text
    return text[:end], text[end:]


def _thinking_prompt_payload(state: AgentState, *, stage: str) -> dict:
    intent = state["intent"] or {}
    plan = state["plan"] or {}
    selected_skill = state["selected_skill"] or {}
    analysis = state["analysis"] or {}
    data_coverage = analysis.get("data_coverage", [])
    return {
        "public_stage": stage,
        "user_message": state["user_message"],
        "intent": {
            "intent": intent.get("intent"),
            "domain": intent.get("domain"),
            "sub_intent": intent.get("sub_intent"),
        },
        "plan": {
            "objective": plan.get("objective"),
            "execution_mode": plan.get("execution_mode"),
            "execution_steps": plan.get("execution_steps", []),
            "retrieval_required": (plan.get("retrieval") or {}).get("required"),
            "tool_repair": _safe_tool_repair_summary(plan.get("tool_repair")),
        },
        "skill": {
            "name": selected_skill.get("skill_name")
            or (plan.get("selected_skill") or {}).get("skill_name"),
            "status": selected_skill.get("status"),
        },
        "data_coverage": [
            {
                "capability": item.get("capability"),
                "status": item.get("status"),
                "missing_optional_fields": item.get("missing_optional_fields", []),
                "error_code": item.get("error_code"),
            }
            for item in data_coverage[:8]
            if isinstance(item, dict)
        ],
        "missing_information": analysis.get("missing_information", [])[:8],
    }


def _safe_tool_repair_summary(tool_repair: object) -> dict | None:
    if not isinstance(tool_repair, dict):
        return None
    return {
        "status": tool_repair.get("status"),
        "strategy": tool_repair.get("strategy"),
        "failed_query": tool_repair.get("failed_query"),
        "recovered_query": tool_repair.get("recovered_query"),
        "observation_error_code": tool_repair.get("observation_error_code"),
    }


def _fallback_thinking_chunks(state: AgentState, *, stage: str = "planning") -> list[str]:
    intent = state["intent"] or {}
    plan = state["plan"] or {}
    if stage == "tool_repair":
        repair = _safe_tool_repair_summary(plan.get("tool_repair")) or {}
        recovered = repair.get("recovered_query")
        content = (
            f"公司实体首次解析没有直接命中，我会结合错误信息尝试别名或上市主体修正。"
            f"当前已验证到更可靠的候选：{recovered}，后续会基于验证后的标的继续取数。"
            if recovered
            else "公司实体首次解析没有直接命中，我会结合错误信息尝试别名或上市主体修正。"
        )
        return list(_chunk_text(content, chunk_size=24))
    if stage == "evidence_review":
        content = "我已经拿到部分工具结果，接下来会核对哪些字段可用、哪些字段缺失，避免把未返回的数据当作事实。"
        return list(_chunk_text(content, chunk_size=24))
    if stage == "answer_synthesis":
        missing = (state["analysis"] or {}).get("missing_information", [])
        content = (
            "我正在把已验证的公司、行情和财务证据整理成最终分析。"
            "如果仍有字段或公告权限缺口，会在结论里明确说明数据边界。"
            if missing
            else "我正在把已验证的公司、行情和财务证据整理成最终分析，并区分事实、判断和数据边界。"
        )
        return list(_chunk_text(content, chunk_size=24))
    if intent.get("domain") == "a_share_equity_research":
        labels = {
            "business": "业务",
            "market": "行情",
            "valuation": "估值",
            "financial": "财务",
            "risk": "风险",
            "technical": "技术面",
        }
        dimensions = [
            labels[item]
            for item in plan.get("analysis_dimensions", [])
            if item in labels
        ]
        if dimensions:
            joined = (
                dimensions[0]
                if len(dimensions) == 1
                else f"{'、'.join(dimensions[:-1])}和{dimensions[-1]}"
            )
            content = (
                f"我会先确认公司实体，再从{joined}等维度核对可用证据，"
                "最后说明主要判断与数据限制。"
            )
        else:
            content = "我会先确认公司实体，再检查可用市场数据和知识证据，最后整理风险与限制。"
    else:
        content = "我会先判断你的目标，再结合可用工具和证据生成回答。"
    return list(_chunk_text(content, chunk_size=24))


def _sync_iter_async_chunks(async_chunks: AsyncIterator[str]) -> Iterator[str]:
    loop = asyncio.new_event_loop()
    try:
        while True:
            try:
                chunk = loop.run_until_complete(anext(async_chunks))
            except StopAsyncIteration:
                break
            if chunk:
                yield chunk
    finally:
        close = getattr(async_chunks, "aclose", None)
        if close is not None:
            loop.run_until_complete(close())
        loop.close()


def _llm_configuration_error_payload(exc: LlmConfigurationError) -> dict:
    return {
        "code": "llm_configuration_error",
        "node": "llm_provider",
        "message": (
            "LLM Provider 配置不完整，请设置 BS_AGENT_DEEPSEEK_API_KEY "
            "后重启本地服务。"
        ),
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


def _record_short_term_session_event(
    *,
    store: ShortTermMemoryStore,
    state: AgentState,
) -> None:
    try:
        final_response = state["final_response"] or {}
        intent = state["intent"] or {}
        store.append_session_event(
            tenant_id=state["tenant_id"],
            session_id=state["session_id"],
            event={
                "request_id": state["request_id"],
                "trace_id": state["trace_id"],
                "intent": intent.get("intent"),
                "approval_status": final_response.get("approval_status"),
                "error_count": len(state["errors"]),
            },
        )
    except Exception:
        return None


def _interrupt_payloads(state: AgentState) -> list[dict]:
    return [interrupt.value for interrupt in state.get("__interrupt__", [])]


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


def _assistant_response_metadata(final_response: dict) -> dict:
    return {
        "cited_evidence_ids": list(final_response.get("cited_evidence_ids", [])),
        "approval_status": final_response.get("approval_status", "not_required"),
        "response_type": final_response.get("response_type"),
        "business_error_code": final_response.get("business_error_code"),
    }


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
