from collections.abc import Callable

from langgraph.runtime import Runtime

from app.graph.planning.equity_research import (
    build_equity_research_evidence_plan,
    has_stock_tooling,
)
from app.graph.planning.templates import (
    build_direct_response_plan,
    build_image_analysis_plan,
    build_image_skill_workflow_plan,
    build_knowledge_qa_plan,
    build_skill_workflow_plan,
)
from app.graph.planning.tool_action import build_tool_action_plan
from app.graph.state import AgentState
from app.llm.factory import LlmClient
from app.memory.candidates import propose_explicit_preference_candidates
from app.memory.preferences import preference_constraints, recall_user_preferences
from app.schemas.tools import ToolSpec
from app.tools.default_registry import list_default_tool_specs


def build_planner_agent_node(
    *,
    llm_client: LlmClient | None = None,
    available_tools: list[ToolSpec] | None = None,
    on_memory_recall_failure: Callable[[AgentState], None] | None = None,
):
    selected_tools = (
        list_default_tool_specs() if available_tools is None else available_tools
    )
    tool_specs = {tool.name: tool for tool in selected_tools}

    def node(state: AgentState, runtime: Runtime | None = None) -> AgentState:
        state["plan"] = _build_plan(
            state=state,
            llm_client=llm_client,
            tool_specs=tool_specs,
        )
        constraints = preference_constraints(
            recall_user_preferences(runtime, state, on_failure=on_memory_recall_failure)
        )
        if constraints:
            state["plan"]["response_policy"].update(
                {key: value for key, value in constraints.items() if key != "focus_domain"}
            )
            state["plan"]["preference_constraints"] = constraints
        state["memory_candidates"] = propose_explicit_preference_candidates(
            user_message=state["user_message"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
        )
        return state

    return node


def planner_agent_node(state: AgentState, runtime: Runtime | None = None) -> AgentState:
    return build_planner_agent_node()(state, runtime)


def _build_plan(
    *,
    state: AgentState,
    llm_client: LlmClient | None,
    tool_specs: dict[str, ToolSpec],
) -> dict:
    intent = state["intent"] or {}
    intent_name = intent.get("intent", "knowledge_qa")

    if (
        state["image_analyses"]
        and intent.get("candidate_skill")
        and intent.get("domain") == "a_share_equity_research"
    ):
        return build_image_skill_workflow_plan(intent)
    if state["image_analyses"]:
        return build_image_analysis_plan(intent)
    if intent_name == "knowledge_qa":
        return build_knowledge_qa_plan(intent)
    if intent_name == "tool_action":
        return build_tool_action_plan(
            state=state,
            llm_client=llm_client,
            tool_specs=tool_specs,
        )
    if (
        intent.get("candidate_skill")
        and intent.get("domain") == "a_share_equity_research"
    ):
        if llm_client is not None and has_stock_tooling(tool_specs):
            return build_equity_research_evidence_plan(
                state=state,
                intent=intent,
                llm_client=llm_client,
                tool_specs=tool_specs,
            )
        return build_skill_workflow_plan(intent)
    return build_direct_response_plan(intent)
