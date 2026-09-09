from collections.abc import Callable

from app.graph.state import AgentState
from app.graph.image_context import summarize_image_analyses
from app.intent.catalog import BusinessIntentCatalog
from app.intent.router import HybridIntentRouter
from app.llm.factory import LlmClient
from app.skills.definitions import SkillCard


def build_intent_router_node(
    *,
    catalog: BusinessIntentCatalog,
    router: HybridIntentRouter | None = None,
    available_skills_provider: Callable[[str], list[SkillCard]] | None = None,
):
    if catalog is None:
        raise TypeError("catalog must not be None")

    selected_router = router
    if selected_router is None:
        selected_router = HybridIntentRouter(
            catalog=catalog,
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
    catalog: BusinessIntentCatalog,
    llm_client: LlmClient | None = None,
    available_skills_provider: Callable[[str], list[SkillCard]] | None = None,
):
    if catalog is None:
        raise TypeError("catalog must not be None")

    return build_intent_router_node(
        catalog=catalog,
        router=HybridIntentRouter(
            catalog=catalog,
            llm_client=llm_client,
            available_skills_provider=available_skills_provider,
        )
    )
