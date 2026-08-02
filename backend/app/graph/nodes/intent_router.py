from app.graph.state import AgentState
from app.graph.image_context import summarize_image_analyses
from app.intent.router import HybridIntentRouter
from app.llm.factory import LlmClient
from app.schemas.tools import ToolSpec
from app.tools.default_registry import list_default_tool_specs


def build_intent_router_node(
    *,
    router: HybridIntentRouter | None = None,
    available_tools: list[ToolSpec] | None = None,
):
    selected_router = router or HybridIntentRouter()
    if router is None and available_tools is not None:
        selected_router = HybridIntentRouter(available_tools=available_tools)

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
    available_tools: list[ToolSpec] | None = None,
):
    return build_intent_router_node(
        router=HybridIntentRouter(
            llm_client=llm_client,
            available_tools=available_tools or list_default_tool_specs(),
        )
    )


def intent_router_node(state: AgentState) -> AgentState:
    return build_intent_router_node()(state)
