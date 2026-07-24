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


def persist_agent_run(
    *,
    session: Session,
    state: AgentState,
    trace_events: Sequence[TraceEvent],
    status: str,
    experience_store: ExperienceStore | None = None,
    commit: bool = True,
) -> AgentRun:
    agent_session = session.get(AgentSession, state["session_id"])
    if agent_session is None:
        agent_session = AgentSession(
            session_id=state["session_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            status="active",
        )
        session.add(agent_session)
    else:
        agent_session.tenant_id = state["tenant_id"]
        agent_session.user_id = state["user_id"]

    intent = state["intent"] or {}
    run = session.get(AgentRun, state["request_id"])
    if run is None:
        run = AgentRun(
            run_id=state["request_id"],
            session_id=state["session_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            trace_id=state["trace_id"],
            status=status,
            intent=intent.get("intent"),
            risk_level=intent.get("risk_level"),
        )
        session.add(run)
    else:
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
            run_id=state["request_id"],
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
    event_ids = {event.event_id for event in trace_events}
    existing_ids = set()
    if event_ids:
        existing_ids.update(
            session.scalars(
                select(AgentTraceEvent.event_id).where(
                    AgentTraceEvent.event_id.in_(event_ids)
                )
            )
        )

    for event in trace_events:
        if event.event_id in existing_ids:
            continue
        try:
            with session.begin_nested():
                session.add(
                    AgentTraceEvent(
                        event_id=event.event_id,
                        trace_id=event.trace_id,
                        run_id=run.run_id,
                        node_name=event.node_name,
                        event_type="node",
                        status=event.status,
                        latency_ms=event.latency_ms,
                        error=event.error,
                    )
                )
                session.flush()
        except IntegrityError:
            # 另一个事务可能已写入同一稳定事件；唯一键将并发重试收敛为幂等成功。
            persisted_event_id = session.scalar(
                select(AgentTraceEvent.event_id).where(
                    AgentTraceEvent.event_id == event.event_id
                )
            )
            if persisted_event_id is None:
                raise
        existing_ids.add(event.event_id)
