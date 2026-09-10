from app.graph.state import AgentState
from app.graph.image_context import summarize_image_analyses
from app.intent.catalog import BusinessIntentCatalog
from app.intent.router import HybridIntentRouter
from app.llm.factory import LlmClient


def build_intent_router_node(
    *,
    catalog: BusinessIntentCatalog,
    llm_client: LlmClient | None,
):
    if catalog is None:
        raise TypeError("catalog must not be None")

    selected_router = HybridIntentRouter(
        catalog=catalog,
        llm_client=llm_client,
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
        state["intent"] = result.model_dump()
        return state

    return node
