from sqlalchemy.orm import Session

from app.graph.state import AgentState
from app.memory.commit import commit_approved_experience_memories
from app.memory.experience_store import ExperienceStore
from app.memory.store import SqlAlchemyMemoryStore
from app.storage.models import AgentRun, AgentSession


def persist_agent_run(
    *,
    session: Session,
    state: AgentState,
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
    candidate_store = SqlAlchemyMemoryStore(session)
    for candidate in state["memory_candidates"]:
        candidate_store.save_candidate(
            candidate,
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            run_id=persisted_run_id,
        )
    if experience_store is not None:
        commit_approved_experience_memories(
            state=state,
            experience_store=experience_store,
        )
    if commit:
        session.commit()
    else:
        session.flush()
    return run
