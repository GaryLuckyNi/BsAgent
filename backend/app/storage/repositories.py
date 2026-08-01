from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.graph.state import AgentState
from app.harness.tracing import TraceEvent
from app.memory.commit import commit_approved_experience_memories
from app.memory.experience_store import ExperienceStore
from app.memory.factory import build_experience_store
from app.memory.store import SqlAlchemyMemoryStore
from app.storage.models import AgentRun, AgentSession, AgentTraceEvent


class TraceEventIntegrityError(RuntimeError):
    code = "trace_event_integrity_error"


def persist_agent_run(
    *,
    session: Session,
    state: AgentState,
    trace_events: Sequence[TraceEvent],
    status: str,
    experience_store: ExperienceStore | None = None,
    commit: bool = True,
    run_id: str | None = None,
    update_run_status: bool = True,
) -> AgentRun:
    session_key = state["session_key"]
    agent_session = session.get(AgentSession, session_key)
    if agent_session is None:
        agent_session = AgentSession(
            session_key=session_key,
            session_id=state["session_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            status="active",
        )
        session.add(agent_session)
    elif (
        agent_session.session_id != state["session_id"]
        or agent_session.tenant_id != state["tenant_id"]
        or agent_session.user_id != state["user_id"]
    ):
        raise ValueError("agent_session_identity_conflict")

    intent = state["intent"] or {}
    persisted_run_id = run_id or state["request_id"]
    run = session.get(AgentRun, persisted_run_id)
    if run is None:
        run = AgentRun(
            run_id=persisted_run_id,
            session_id=state["session_id"],
            session_key=session_key,
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            trace_id=state["trace_id"],
            status=status,
            intent=intent.get("intent"),
            risk_level=intent.get("risk_level"),
        )
        session.add(run)
    else:
        if (
            run.session_key != session_key
            or run.session_id != state["session_id"]
            or run.tenant_id != state["tenant_id"]
            or run.user_id != state["user_id"]
            or run.trace_id != state["trace_id"]
        ):
            raise ValueError("agent_run_identity_conflict")
        if update_run_status:
            run.status = status
        run.intent = intent.get("intent")
        run.risk_level = intent.get("risk_level")
    _persist_trace_events(session=session, run=run, trace_events=trace_events)
    candidate_store = SqlAlchemyMemoryStore(session)
    for candidate in state["memory_candidates"]:
        candidate_store.save_candidate(
            candidate,
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            run_id=persisted_run_id,
        )
    commit_approved_experience_memories(
        state=state,
        experience_store=experience_store or build_experience_store(session=session),
    )
    if commit:
        session.commit()
    else:
        session.flush()
    return run


def _persist_trace_events(
    *,
    session: Session,
    run: AgentRun,
    trace_events: Sequence[TraceEvent],
) -> None:
    for event in trace_events:
        if event.trace_id != run.trace_id:
            raise TraceEventIntegrityError(
                f"Trace event {event.event_id} belongs to {event.trace_id}, "
                f"but run {run.run_id} belongs to {run.trace_id}."
            )

    event_ids = {event.event_id for event in trace_events}
    existing_events: dict[str, AgentTraceEvent] = {}
    if event_ids:
        existing_events.update(
            (
                event.event_id,
                event,
            )
            for event in session.scalars(
                select(AgentTraceEvent).where(
                    AgentTraceEvent.event_id.in_(event_ids)
                )
            )
        )

    for event in trace_events:
        existing_event = existing_events.get(event.event_id)
        if existing_event is not None:
            _validate_existing_trace_event(
                existing_event=existing_event,
                event=event,
                run=run,
            )
            continue
        try:
            with session.begin_nested():
                stored_event = AgentTraceEvent(
                    event_id=event.event_id,
                    trace_id=event.trace_id,
                    run_id=run.run_id,
                    node_name=event.node_name,
                    event_type="node",
                    status=event.status,
                    latency_ms=event.latency_ms,
                    error=event.error,
                )
                session.add(stored_event)
                session.flush()
        except IntegrityError:
            # 另一个事务可能已写入同一稳定事件；唯一键将并发重试收敛为幂等成功。
            stored_event = session.scalar(
                select(AgentTraceEvent).where(
                    AgentTraceEvent.event_id == event.event_id
                )
            )
            if stored_event is None:
                raise
            _validate_existing_trace_event(
                existing_event=stored_event,
                event=event,
                run=run,
            )
        existing_events[event.event_id] = stored_event


def _validate_existing_trace_event(
    *,
    existing_event: AgentTraceEvent,
    event: TraceEvent,
    run: AgentRun,
) -> None:
    expected = (
        run.run_id,
        run.trace_id,
        event.node_name,
        "node",
        event.status,
        event.latency_ms,
        event.error,
    )
    actual = (
        existing_event.run_id,
        existing_event.trace_id,
        existing_event.node_name,
        existing_event.event_type,
        existing_event.status,
        existing_event.latency_ms,
        existing_event.error,
    )
    if actual != expected:
        raise TraceEventIntegrityError(
            f"Trace event {event.event_id} conflicts with its persisted record."
        )
