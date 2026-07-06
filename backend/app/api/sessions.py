import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.graph.checkpoints import build_graph_checkpointer
from app.graph.runtime import AgentGraphExecutionError, run_question_answer_graph
from app.graph.state import AgentState
from app.core.config import settings
from app.harness.tracing import get_trace_store
from app.llm.factory import maybe_build_llm_client
from app.memory.factory import build_experience_store
from app.memory.retriever import ExperienceMemoryRetriever
from app.memory.short_term_factory import build_short_term_memory_store
from app.memory.short_term_store import ShortTermMemoryStore
from app.storage.database import get_db
from app.storage.repositories import persist_agent_run

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionMessageRequest(BaseModel):
    tenant_id: str
    user_id: str
    message: str


class SessionMessageResponse(BaseModel):
    content: str
    trace_id: str
    cited_evidence_ids: list[str]
    approval_status: str


@router.post("/{session_id}/messages", response_model=SessionMessageResponse)
def post_message(
    session_id: str,
    request: SessionMessageRequest,
    db: Session = Depends(get_db),
) -> SessionMessageResponse:
    try:
        state = _run_and_persist_agent_message(
            session_id=session_id,
            request=request,
            db=db,
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

    return SessionMessageResponse(**state["final_response"])


@router.post("/{session_id}/messages/stream")
def post_message_stream(
    session_id: str,
    request: SessionMessageRequest,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    return StreamingResponse(
        _stream_agent_message(session_id=session_id, request=request, db=db),
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
    db: Session,
) -> AgentState:
    trace_store = get_trace_store()
    experience_store = build_experience_store(session=db)
    state = run_question_answer_graph(
        session_id=session_id,
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        user_message=request.message,
        mock_evidence=settings.use_mock_rag,
        memory_retriever=ExperienceMemoryRetriever(experience_store),
        trace_store=trace_store,
        checkpointer=build_graph_checkpointer(),
        llm_client=maybe_build_llm_client(),
    )

    persist_agent_run(
        session=db,
        state=state,
        trace_events=trace_store.list_events(state["trace_id"]),
        status="completed",
        experience_store=experience_store,
    )
    _record_short_term_session_event(
        store=build_short_term_memory_store(),
        state=state,
    )
    return state


def _stream_agent_message(
    *,
    session_id: str,
    request: SessionMessageRequest,
    db: Session,
) -> Iterator[str]:
    yield _sse("status", {"stage": "accepted"})
    try:
        state = _run_and_persist_agent_message(
            session_id=session_id,
            request=request,
            db=db,
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
    for chunk in _chunk_text(final_response["content"]):
        yield _sse("answer_delta", {"content": chunk})
    yield _sse("final", final_response)
    yield _sse("done", {"ok": True})


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunk_text(content: str, *, chunk_size: int = 12) -> Iterator[str]:
    for index in range(0, len(content), chunk_size):
        yield content[index : index + chunk_size]


def _trace_event_payload(event) -> dict:
    return {
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
