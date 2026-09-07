from collections.abc import Callable

from app.graph.state import AgentState
from app.graph.image_context import summarize_image_analyses
from app.intent.router import HybridIntentRouter
from app.llm.factory import LlmClient
from app.skills.definitions import SkillCard


def build_intent_router_node(
    *,
    router: HybridIntentRouter | None = None,
    available_skills_provider: Callable[[str], list[SkillCard]] | None = None,
):
    selected_router = router or HybridIntentRouter(
        available_skills_provider=available_skills_provider,
    )

    def node(state: AgentState) -> AgentState:
        decision = selected_router.route(
            message=state["user_message"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            context={
                "image_analyses": summarize_image_analyses(
                    state["image_analyses"]
                )
            },
        )
        state["intent"] = decision.model_dump()
        return state

    return node


def build_llm_intent_router_node(
    *,
    llm_client: LlmClient | None = None,
    available_skills_provider: Callable[[str], list[SkillCard]] | None = None,
):
    return build_intent_router_node(
        router=HybridIntentRouter(
            llm_client=llm_client,
            available_skills_provider=available_skills_provider,
        )
    )


def intent_router_node(state: AgentState) -> AgentState:
    return build_intent_router_node()(state)
