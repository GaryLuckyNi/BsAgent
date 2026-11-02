from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.graph.image_context import summarize_image_analyses
from app.graph.nodes.synthesizer import _draft
from app.graph.planning.context import result_catalog
from app.graph.public_events import build_public_field_stream
from app.graph.state import (
    AgentState,
    context_compaction_payload,
    is_context_compaction_message,
)
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
        progress = build_public_field_stream("intent_router", state)
        try:
            result = selected_router.route(
                message=state["user_message"],
                tenant_id=state["tenant_id"],
                user_id=state["user_id"],
                context={
                    "context_summary": _context_summary(state),
                    "conversation_history": _recent_conversation_history(state),
                    "image_analyses": summarize_image_analyses(state["image_analyses"]),
                    "available_results": result_catalog(state),
                },
                on_delta=progress,
            )
        except Exception:
            if progress is not None:
                progress.fail()
            raise
        state["intent"] = result.model_dump()
        # 旧提供方响应缺路由时保守保留旧语义；恢复中的旧运行不会重新经过入口。
        if "route" in result.model_fields_set:
            state["request_flow_version"] = "context-routing-v1"
        if result.route == "direct_answer":
            state["draft_answer"] = _draft(result.answer, response_type="research_answer")
        return state

    return node


def _context_summary(state: AgentState) -> dict | None:
    for message in state["messages"]:
        payload = context_compaction_payload(message)
        if payload is not None:
            return payload
    return None


def _recent_conversation_history(state: AgentState, *, limit: int = 8) -> list[dict]:
    history: list[dict] = []
    for message in reversed(state["messages"]):
        if is_context_compaction_message(message) or isinstance(message, ToolMessage):
            continue
        if isinstance(message, AIMessage) and (
            message.tool_calls or message.additional_kwargs.get("task_role")
        ):
            continue
        metadata = message.additional_kwargs.get("metadata", {})
        if isinstance(message, HumanMessage) and isinstance(metadata, dict) and (
            metadata.get("request_id") == state["request_id"]
        ):
            continue
        if isinstance(message, (HumanMessage, AIMessage)):
            history.append({"ref": f"message:{message.id}", "role": message.type, "content": str(message.content)})
        if len(history) >= limit:
            break
    history.reverse()
    return history
