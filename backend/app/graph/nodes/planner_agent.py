from collections.abc import Callable
from time import perf_counter

from langgraph.runtime import Runtime

from app.core.config import settings
from app.graph.planning.context import request_context, user_materials
from app.graph.planning.contracts import PlanModel, SubTaskModel, TaskResultModel
from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities, PlannerOutputError
from app.graph.planning.policy import (
    MAX_PLANNER_ITERATIONS,
    observation_signature,
    planner_mode,
    remaining_planner_iterations,
)
from app.graph.planning.request_flow import pending_replan_valid, uses_request_flow
from app.graph.state import AgentState
from app.llm.factory import LlmClient
from app.memory.candidates import propose_explicit_preference_candidates
from app.memory.preferences import planning_preference_constraints, recall_user_preferences
from app.operations.langfuse_observability import langfuse_observation, update_langfuse_observation
from app.skills.registry import SkillRegistry
from app.tools.planning_candidates import recall_candidates
from app.tools.search import ToolSearchService


def build_planner_agent_node(
    *,
    llm_client: LlmClient | None = None,
    skill_registry: SkillRegistry | None = None,
    tool_search_service: ToolSearchService | None = None,
    on_memory_recall_failure: Callable[[AgentState], None] | None = None,
):
    def node(state: AgentState, runtime: Runtime | None = None) -> AgentState:
        new_flow = uses_request_flow(state)
        if new_flow and state.get("plan") is not None and not pending_replan_valid(state):
            state["pending_replan"] = None
            state["replan_stop_reason"] = "replan_request_unbound"
            return state
        if llm_client is None:
            raise RuntimeError("Planner requires an LLM client.")
        intent = state.get("intent") or {}
        intent_type = intent.get("intent_type")
        domain = intent.get("domain")
        subdomain = intent.get("subdomain")
        goal = intent.get("goal")
        if not all(
            isinstance(value, str) and value
            for value in (
                intent_type,
                domain,
                subdomain,
                goal,
            )
        ):
            raise ValueError("Planner requires an in-scope intent decision and goal.")

        capabilities = _discover_capabilities(
            state=state,
            domain=domain,
            subdomain=subdomain,
            skill_registry=skill_registry,
            tool_search_service=tool_search_service,
        )
        constraints = planning_preference_constraints(
            recall_user_preferences(runtime, state, on_failure=on_memory_recall_failure)
        )
        materials = user_materials(state, include_current_message=False)
        if materials:
            constraints = {**constraints, "user_materials": materials}
        if new_flow or (state.get("intent") or {}).get("available_information"):
            constraints = {**constraints, "request_context": request_context(state)}
            constraints.pop("user_materials", None)
        current_iteration = state["planner_iteration"]
        if state.get("plan") is not None:
            current_plan = PlanModel.model_validate(state["plan"])
            try:
                decision = LlmTaskPlanner(llm_client=llm_client).review(
                    goal=goal,
                    user_message=state["user_message"],
                    current_plan=current_plan,
                    capabilities=capabilities,
                    task_results=tuple(
                        TaskResultModel.model_validate(item)
                        for item in state.get("task_results", [])
                    ),
                    remaining_revisions=max(
                        0,
                        min(settings.planner_max_iterations, MAX_PLANNER_ITERATIONS)
                        - current_iteration,
                    ),
                    planning_constraints=constraints,
                    **({"replan_request": state["pending_replan"]["request"]} if new_flow else {}),
                )
            except PlannerOutputError:
                if new_flow:
                    state["pending_replan"] = None
                    state["replan_stop_reason"] = "replan_output_invalid"
                state["planner_stop_reason"] = "planner_feedback_invalid"
                state["planner_decision"] = None
                state["planner_feedback_count"] = state.get("planner_feedback_count", 0) + 1
                state["planner_reviewed_signature"] = observation_signature(
                    current_plan, state.get("task_results", [])
                )
                return state
            if decision.plan is not None:
                state["plan_history"] = [
                    *state.get("plan_history", []),
                    {
                        "revision": current_iteration - 1,
                        "plan": current_plan.model_dump(mode="json"),
                    },
                ]
                completed = {item["task_id"] for item in state.get("task_results", [])}
                plan = _namespace_replanned_tasks(
                    decision.plan, revision=current_iteration, preserved_ids=completed
                )
                state["plan"] = plan.model_dump(mode="python")
                state["planner_iteration"] = current_iteration + 1
            else:
                plan = current_plan
            state["planner_decision"] = {
                "action": decision.action,
                "reason": decision.reason,
                "unresolved_questions": decision.unresolved_questions,
            }
            if decision.synthesis_task_id is not None:
                state["planner_decision"]["synthesis_task_id"] = decision.synthesis_task_id
            state["planner_feedback_count"] = state.get("planner_feedback_count", 0) + 1
            state["planner_reviewed_signature"] = observation_signature(
                plan, state.get("task_results", [])
            )
            if new_flow:
                state["pending_replan"] = None
                state["synthesis_decision"] = None
                if decision.plan is None:
                    state["replan_stop_reason"] = "replan_no_effective_change"
            return state
        mode = planner_mode(current_iteration)
        plan = LlmTaskPlanner(llm_client=llm_client).plan(
            goal=goal,
            intent_type=intent_type,
            domain=domain,
            subdomain=subdomain,
            capabilities=capabilities,
            task_results=tuple(
                TaskResultModel.model_validate(item) for item in state.get("task_results", [])
            ),
            remaining_iterations=remaining_planner_iterations(current_iteration),
            planning_constraints={**constraints, "user_message": state["user_message"]},
            mode=mode,
        )
        if current_iteration:
            plan = _namespace_replanned_tasks(plan, revision=current_iteration)
        plan_payload = plan.model_dump(mode="python")
        if plan_payload.get("public_summary") is None:
            plan_payload.pop("public_summary", None)
        state["plan"] = plan_payload
        state["planner_iteration"] = current_iteration + 1
        state["memory_candidates"] = propose_explicit_preference_candidates(
            user_message=state["user_message"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
        )
        return state

    return node


def _namespace_replanned_tasks(
    plan: PlanModel, *, revision: int, preserved_ids: set[str] | None = None
) -> PlanModel:
    from hashlib import sha256

    preserved_ids = preserved_ids or set()

    def revised_id(task_id: str) -> str:
        name = f"r{revision}_{task_id}"
        return name if len(name) <= 64 else f"{name[:51]}_{sha256(name.encode()).hexdigest()[:12]}"

    names = {}
    occupied = set(preserved_ids)
    for task in plan.tasks:
        if task.id in preserved_ids:
            names[task.id] = task.id
            continue
        name = revised_id(task.id)
        attempt = 0
        while name in occupied:
            attempt += 1
            suffix = sha256(f"{revision}:{task.id}:{attempt}".encode()).hexdigest()[:12]
            name = f"{revised_id(task.id)[:51]}_{suffix}"
        names[task.id] = name
        occupied.add(name)
    return PlanModel(
        public_summary=plan.public_summary,
        tasks=[
            SubTaskModel.model_validate(
                {
                    **task.model_dump(),
                    "id": names[task.id],
                    "depends_on": [names[dependency] for dependency in task.depends_on],
                }
            )
            for task in plan.tasks
        ],
    )


def _discover_capabilities(
    *,
    state: AgentState,
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
        skill_cards = tuple(sorted(eligible_cards, key=lambda card: card.name))
    started = perf_counter()
    with langfuse_observation(as_type="span", name="planning_tool_recall") as observation:
        snapshot = recall_candidates(state=state, search_service=tool_search_service,
                                     top_k=settings.tool_search_top_k)
        update_langfuse_observation(observation, bounded_content=True, output={
            "catalog_digest": snapshot.catalog_digest,
            "query_fingerprint": snapshot.query_fingerprint,
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "candidate_count": len(snapshot.candidates),
            "candidates": [{"ref": card.ref, "sources": card.sources} for card in snapshot.candidates],
            "truncated": snapshot.truncated, "reason": snapshot.reason,
        })
    state["planning_tool_candidates"] = snapshot.model_dump(mode="json")
    return PlannerCapabilities(skills=skill_cards, tools=tuple(snapshot.candidates))


def planner_agent_node(state: AgentState, runtime: Runtime | None = None) -> AgentState:
    return build_planner_agent_node()(state, runtime)
