from app.graph.state import AgentState
from app.memory.commit import commit_approved_experience_memories
from app.memory.experience_store import ExperienceStore
from app.schemas.approvals import ApprovalStatus


def build_approval_coordinator_node(
    *,
    experience_store: ExperienceStore | None = None,
):
    def node(state: AgentState) -> AgentState:
        verification = state["verification"] or {}
        if verification.get("verdict") == "execution_error":
            state["approval_status"] = ApprovalStatus(
                required=False,
                status="not_required",
            )
            return state
        requires_approval = verification.get("verdict") == "needs_approval"
        existing_status = state["approval_status"]
        if existing_status is not None and existing_status.status in {
            "approved",
            "rejected",
        }:
            state["approval_status"] = ApprovalStatus(
                required=True,
                status=existing_status.status,
            )
        else:
            state["approval_status"] = ApprovalStatus(
                required=requires_approval,
                status="pending" if requires_approval else "not_required",
            )
        if experience_store is not None:
            commit_approved_experience_memories(
                state=state,
                experience_store=experience_store,
            )
        return state

    return node


def approval_coordinator_node(state: AgentState) -> AgentState:
    return build_approval_coordinator_node()(state)
