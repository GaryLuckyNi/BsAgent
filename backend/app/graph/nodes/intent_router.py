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
    llm_client: LlmClient | None,
    available_skills_provider: Callable[[str], list[SkillCard]] | None,
):
    if catalog is None:
        raise TypeError("catalog must not be None")

    selected_router = HybridIntentRouter(
        catalog=catalog,
        llm_client=llm_client,
        available_skills_provider=available_skills_provider,
    )

    def node(state: AgentState) -> AgentState:
        result = selected_router.route(
            message=state["user_message"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            context={
                "image_analyses": summarize_image_analyses(
                    state["image_analyses"]
                )
            },
        )
        state["intent"] = result.decision.model_dump()
        state["task_brief"] = (
            result.task_brief.model_dump() if result.task_brief else None
        )
        return state

    return node
