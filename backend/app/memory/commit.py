from app.graph.state import AgentState
from app.memory.experience_store import ExperienceStore
from app.schemas.memory import ExperienceCommitPayload


def commit_approved_experience_memories(
    *,
    state: AgentState,
    experience_store: ExperienceStore,
) -> None:
    candidates_by_id = {
        candidate.candidate_id: candidate for candidate in state["memory_candidates"]
    }
    for request in state["approval_requests"]:
        if request.approval_type != "memory_write" or request.status != "approved":
            continue
        if request.payload.get("experience_committed") is True:
            continue
        candidate = candidates_by_id.get(request.payload.get("candidate_id"))
        if candidate is None or candidate.approval_status != "approved":
            continue
        experience_store.save(
            ExperienceCommitPayload(
                candidate_id=candidate.candidate_id,
                tenant_id=state["tenant_id"],
                user_id=state["user_id"],
                memory_type=candidate.memory_type,
                content=candidate.content,
                sensitivity=candidate.sensitivity,
                source=candidate.source,
            )
        )
        request.payload["experience_committed"] = True
