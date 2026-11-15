from app.graph.state import AgentState
from app.schemas.approvals import ApprovalStatus


def build_approval_coordinator_node():
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
        return state

    return node


def approval_coordinator_node(state: AgentState) -> AgentState:
    return build_approval_coordinator_node()(state)
