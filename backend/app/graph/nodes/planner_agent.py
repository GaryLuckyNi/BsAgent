from collections.abc import Callable

from langgraph.runtime import Runtime

from app.core.config import settings
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
from app.memory.preferences import (
    planning_preference_constraints,
    recall_user_preferences,
)
from app.schemas.tools import ToolSpec
from app.skills.definitions import SkillCard
from app.tools.search import ToolSearchContext, ToolSearchInput, ToolSearchService


def build_planner_agent_node(
    *,
    llm_client: LlmClient | None = None,
    tool_search_service: ToolSearchService | None = None,
    available_skills_provider: Callable[[str], list[SkillCard]] | None = None,
    on_memory_recall_failure: Callable[[AgentState], None] | None = None,
):
    def node(state: AgentState, runtime: Runtime | None = None) -> AgentState:
        tool_specs = _disclosed_tool_specs(
            state=state,
            service=tool_search_service,
            available_skills_provider=available_skills_provider,
        )
        state["plan"] = _build_plan(
            state=state,
            llm_client=llm_client,
            tool_specs=tool_specs,
        )
        constraints = planning_preference_constraints(
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


def _disclosed_tool_specs(
    *,
    state: AgentState,
    service: ToolSearchService | None,
    available_skills_provider: Callable[[str], list[SkillCard]] | None,
) -> dict[str, ToolSpec]:
    if service is None or not _should_prefetch_tools(state):
        return {}
    if not state["disclosed_tool_names"]:
        response = service.search(
            ToolSearchInput(
                query=_build_initial_tool_query(
                    state,
                    available_skills_provider=available_skills_provider,
                ),
                top_k=settings.tool_search_top_k,
            ),
            context=ToolSearchContext(
                tenant_id=state["tenant_id"],
                user_id=state["user_id"],
                permission_scopes=tuple(state["permission_scopes"]),
            ),
        )
        state["disclosed_tool_names"] = _merge_disclosed_names(
            state["disclosed_tool_names"],
            [hit.tool_name for hit in response.hits],
            limit=settings.tool_search_max_disclosed_tools,
        )
    specs = {
        spec.name: spec
        for spec in service.resolve_specs(state["disclosed_tool_names"])
    }
    try:
        search_spec = service.resolve_specs(["search_tool"])[0]
    except (KeyError, IndexError):
        return specs
    specs[search_spec.name] = search_spec
    return specs


def _should_prefetch_tools(state: AgentState) -> bool:
    intent = state["intent"] or {}
    return bool(
        intent.get("intent") == "tool_action"
        or (
            intent.get("candidate_skill")
            and intent.get("domain") == "a_share_equity_research"
        )
    )


def _build_initial_tool_query(
    state: AgentState,
    *,
    available_skills_provider: Callable[[str], list[SkillCard]] | None,
) -> str:
    intent = state["intent"] or {}
    parts = [
        state["user_message"],
        str(intent.get("domain") or ""),
        str(intent.get("sub_intent") or ""),
    ]
    candidate_skill = intent.get("candidate_skill")
    if candidate_skill and available_skills_provider is not None:
        try:
            skill = next(
                (
                    item
                    for item in available_skills_provider(state["tenant_id"])
                    if item.name == candidate_skill
                ),
                None,
            )
        except Exception:
            skill = None
        if skill is not None:
            parts.append(skill.summary)
    return " ".join(part for part in parts if part)


def _merge_disclosed_names(
    existing: list[str],
    incoming: list[str],
    *,
    limit: int,
) -> list[str]:
    return list(dict.fromkeys([*existing, *incoming]))[:limit]


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
