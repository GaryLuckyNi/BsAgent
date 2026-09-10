from collections.abc import Callable

from langgraph.runtime import Runtime

from app.core.config import settings
from app.graph.planning.contracts import PlanModel, SubTaskModel, TaskResultModel
from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities
from app.graph.state import AgentState, merge_disclosed_tool_names
from app.llm.factory import LlmClient
from app.memory.candidates import propose_explicit_preference_candidates
from app.memory.preferences import planning_preference_constraints, recall_user_preferences
from app.schemas.tools import ToolSpec
from app.skills.registry import SkillRegistry
from app.skills.search import SkillSearchService
from app.tools.search import (
    ToolSearchContext,
    ToolSearchHit,
    ToolSearchInput,
    ToolSearchService,
)


def build_planner_agent_node(
    *,
    llm_client: LlmClient | None = None,
    skill_registry: SkillRegistry | None = None,
    tool_search_service: ToolSearchService | None = None,
    on_memory_recall_failure: Callable[[AgentState], None] | None = None,
):
    def node(state: AgentState, runtime: Runtime | None = None) -> AgentState:
        if llm_client is None:
            raise RuntimeError("Planner requires an LLM client.")
        intent = state.get("intent") or {}
        intent_type = intent.get("intent_type")
        domain = intent.get("domain")
        subdomain = intent.get("subdomain")
        goal = intent.get("goal")
        if not all(isinstance(value, str) and value for value in (
            intent_type,
            domain,
            subdomain,
            goal,
        )):
            raise ValueError("Planner requires an in-scope intent decision and goal.")

        capabilities = _discover_capabilities(
            state=state,
            goal=goal,
            domain=domain,
            subdomain=subdomain,
            skill_registry=skill_registry,
            tool_search_service=tool_search_service,
        )
        state["disclosed_tool_names"] = merge_disclosed_tool_names(
            state["disclosed_tool_names"],
            [hit.tool_name for hit in capabilities.tools],
            limit=settings.tool_search_max_disclosed_tools,
        )
        constraints = planning_preference_constraints(
            recall_user_preferences(runtime, state, on_failure=on_memory_recall_failure)
        )
        current_iteration = state["planner_iteration"]
        plan = LlmTaskPlanner(llm_client=llm_client).plan(
            goal=goal,
            intent_type=intent_type,
            domain=domain,
            subdomain=subdomain,
            capabilities=capabilities,
            task_results=tuple(
                TaskResultModel.model_validate(item)
                for item in state.get("task_results", [])
            ),
            remaining_iterations=max(
                settings.planner_max_iterations - current_iteration,
                0,
            ),
            planning_constraints=constraints,
        )
        if current_iteration:
            plan = _namespace_replanned_tasks(plan, revision=current_iteration)
        state["plan"] = plan.model_dump(mode="python")
        state["planner_iteration"] = current_iteration + 1
        state["memory_candidates"] = propose_explicit_preference_candidates(
            user_message=state["user_message"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
        )
        return state

    return node


def _namespace_replanned_tasks(plan: PlanModel, *, revision: int) -> PlanModel:
    names = {task.id: f"r{revision}_{task.id}" for task in plan.tasks}
    return PlanModel(
        tasks=[
            SubTaskModel(
                id=names[task.id],
                task_goal=task.task_goal,
                depends_on=[names[dependency] for dependency in task.depends_on],
                skill_name=task.skill_name,
                tool_name=task.tool_name,
            )
            for task in plan.tasks
        ]
    )


def _discover_capabilities(
    *,
    state: AgentState,
    goal: str,
    domain: str,
    subdomain: str,
    skill_registry: SkillRegistry | None,
    tool_search_service: ToolSearchService | None,
) -> PlannerCapabilities:
    skill_cards = ()
    if skill_registry is not None:
        eligible_cards = skill_registry.discover(
            domain=domain,
            subdomain=subdomain,
        )
        skill_cards = tuple(
            SkillSearchService(eligible_cards).search(
                query=goal,
                domain=domain,
                subdomain=subdomain,
                top_k=settings.skill_search_top_k,
            )
        )

    tool_hits: list[ToolSearchHit] = []
    if tool_search_service is not None:
        response = tool_search_service.search(
            ToolSearchInput(query=goal, top_k=settings.tool_search_top_k),
            context=ToolSearchContext(
                tenant_id=state["tenant_id"],
                user_id=state["user_id"],
                permission_scopes=tuple(state["permission_scopes"]),
            ),
        )
        tool_hits.extend(response.hits)
        previously_disclosed = state.get("disclosed_tool_names", [])
        for spec in (
            tool_search_service.resolve_specs(previously_disclosed)
            if previously_disclosed
            else []
        ):
            if all(hit.tool_name != spec.name for hit in tool_hits):
                tool_hits.append(_tool_card(spec, rank=len(tool_hits) + 1))
        search_hit = _search_tool_hit(tool_search_service, rank=len(tool_hits) + 1)
        if search_hit is not None and all(
            hit.tool_name != search_hit.tool_name for hit in tool_hits
        ):
            tool_hits.append(search_hit)
    return PlannerCapabilities(skills=skill_cards, tools=tuple(tool_hits))


def _search_tool_hit(
    service: ToolSearchService,
    *,
    rank: int,
) -> ToolSearchHit | None:
    try:
        spec = service.resolve_specs(["search_tool"])[0]
    except (KeyError, IndexError):
        return None
    return _tool_card(spec, rank=rank)


def _tool_card(spec: ToolSpec, *, rank: int) -> ToolSearchHit:
    required = spec.input_schema.get("required", [])
    return ToolSearchHit(
        tool_name=spec.name,
        tool_version=spec.version,
        description=spec.description,
        required_arguments=(
            [item for item in required if isinstance(item, str)]
            if isinstance(required, list)
            else []
        ),
        rank=rank,
        sources=["exact"],
    )


def planner_agent_node(state: AgentState, runtime: Runtime | None = None) -> AgentState:
    return build_planner_agent_node()(state, runtime)
