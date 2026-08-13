from app.graph.state import AgentState
from app.memory.experience_store import ExperienceStore
from app.schemas.memory import ExperienceCommitPayload, MemoryCandidate


def commit_approved_experience_memories(
    *,
    state: AgentState,
    experience_store: ExperienceStore,
) -> None:
    candidates_by_id = {
        candidate.candidate_id: candidate for candidate in state["memory_candidates"]
    }
    committed_candidate_ids = {
        request.payload.get("candidate_id")
        for request in state["approval_requests"]
        if request.approval_type == "memory_write"
        and request.payload.get("experience_committed") is True
    }
    for candidate in state["memory_candidates"]:
        if (
            candidate.approval_status == "not_required"
            and candidate.sensitivity == "low"
            and candidate.candidate_id not in committed_candidate_ids
        ):
            experience_store.save(_commit_payload(state, candidate))

    for request in state["approval_requests"]:
        if request.approval_type != "memory_write" or request.status != "approved":
            continue
        if request.payload.get("experience_committed") is True:
            continue
        candidate = candidates_by_id.get(request.payload.get("candidate_id"))
        if candidate is None or candidate.approval_status != "approved":
            continue
        experience_store.save(_commit_payload(state, candidate))
        request.payload["experience_committed"] = True


def _commit_payload(
    state: AgentState,
    candidate: MemoryCandidate,
) -> ExperienceCommitPayload:
    return ExperienceCommitPayload(
        candidate_id=candidate.candidate_id,
        tenant_id=state["tenant_id"],
        user_id=state["user_id"],
        memory_type=candidate.memory_type,
        memory_key=candidate.memory_key,
        content=candidate.content,
        sensitivity=candidate.sensitivity,
        source=candidate.source,
    )
