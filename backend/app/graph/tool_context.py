from collections.abc import Sequence

from app.graph.state import AgentState
from app.tools.execution_gateway import ToolExecutionContext


DEFAULT_RETRIEVAL_ALLOWED_TOOLS = ("rag_search",)


def build_tool_execution_context(
    state: AgentState,
    *,
    default_allowed_tools: Sequence[str] = (),
) -> ToolExecutionContext:
    plan = state["plan"] or {}
    tool_policy = plan.get("tool_policy")
    if isinstance(tool_policy, dict) and "allowed_tools" in tool_policy:
        allowed_tools = tool_policy["allowed_tools"]
    else:
        allowed_tools = default_allowed_tools

    selected_skill = state["selected_skill"]
    skill_allowed_tools = None
    if selected_skill is not None and "allowed_tools" in selected_skill:
        skill_allowed_tools = selected_skill["allowed_tools"]

    return ToolExecutionContext(
        tenant_id=state["tenant_id"],
        user_id=state["user_id"],
        session_id=state["session_id"],
        allowed_tools=allowed_tools,
        skill_name=selected_skill.get("skill_name") if selected_skill else None,
        skill_allowed_tools=skill_allowed_tools,
        permission_scopes=state["permission_scopes"],
        approved_tool_calls=state.get("approved_tool_calls", []),
    )
