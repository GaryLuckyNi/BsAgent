from collections.abc import Callable

from langgraph.graph import END, StateGraph
from langgraph.types import Checkpointer, Command

from app.graph.nodes.analysis_agent import analysis_agent_node
from app.graph.nodes.approval_gate import approval_gate_node
from app.graph.nodes.approval_coordinator import approval_coordinator_node
from app.graph.nodes.intent_router import build_llm_intent_router_node
from app.graph.nodes.memory_manager import MemoryRetriever
from app.graph.nodes.memory_manager import MemoryManager
from app.graph.nodes.memory_manager import build_memory_manager_node
from app.graph.nodes.memory_manager import memory_manager_node
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.nodes.retriever_agent import build_default_rag_retriever_node
from app.graph.nodes.skill_agent import (
    guard_skill_agent_node,
    skill_agent_node as default_skill_agent_node,
)
from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.nodes.verifier import verifier_node
from app.graph.state import AgentState, create_initial_state
from app.harness.node_runner import NodeRunner
from app.harness.tracing import InMemoryTraceStore
from app.llm.factory import LlmClient
from app.schemas.evidence import EvidenceItem, EvidencePackage
from app.schemas.tools import ToolCallRequest
from app.tools.default_registry import get_default_tool_registry
from app.tools.registry import ToolRegistry


class AgentGraphExecutionError(RuntimeError):
    def __init__(
        self,
        *,
        node_name: str,
        message: str,
        code: str = "agent_graph_execution_failed",
        http_status: int = 500,
    ) -> None:
        self.node_name = node_name
        self.message = message
        self.code = code
        self.http_status = http_status
        super().__init__(f"{node_name}: {message}")


def _mock_retrieval_node(state: AgentState) -> AgentState:
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_mock",
        query_set=[state["user_message"]],
        coverage="sufficient",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                source_uri="kb://finance",
                title="差旅制度",
                content="差旅报销需要提交发票和审批单。",
                score=0.95,
            )
        ],
    )
    state["ready_tool_call"] = None
    return state


def final_response_node(state: AgentState) -> AgentState:
    draft = state["draft_answer"]
    approval_status = state["approval_status"]
    state["final_response"] = {
        "content": draft["content"] if draft else "没有生成答案。",
        "trace_id": state["trace_id"],
        "cited_evidence_ids": draft.get("cited_evidence_ids", []) if draft else [],
        "approval_status": approval_status.status if approval_status else "not_required",
    }
    return state


def _with_harness(
    node_name: str,
    node: Callable[[AgentState], AgentState],
    runner: NodeRunner,
) -> Callable[[AgentState], AgentState]:
    def wrapped(state: AgentState) -> AgentState:
        previous_error_count = len(state["errors"])
        result = runner.run(node_name, node, state)
        if len(result["errors"]) > previous_error_count:
            error = result["errors"][-1]
            if error.get("handled"):
                return result
            raise AgentGraphExecutionError(
                node_name=error["node"],
                message=error["message"],
                code=error.get("code", "agent_graph_execution_failed"),
                http_status=error.get("http_status", 500),
            )
        return result

    return wrapped


def _route_after_planner(state: AgentState) -> str:
    plan = state["plan"] or {}
    if plan.get("execution_mode") == "direct_response":
        return "synthesizer"
    if plan.get("execution_mode") == "tool_execution":
        return "tool_agent"
    if _has_pending_tool_approval(state):
        return "approval_gate"
    return "skill_agent"


def _route_after_skill_agent(state: AgentState) -> str:
    if _requested_tools(state):
        return "tool_agent"
    if _has_pending_tool_approval(state):
        return "approval_gate"
    return "synthesizer"


def _route_after_tool_agent(state: AgentState) -> str:
    if _has_pending_tool_approval(state):
        return "approval_gate"
    if _has_ready_rag_call(state):
        return "retriever_agent"
    return "synthesizer"


def _route_after_approval_gate(state: AgentState) -> str:
    if state["approval_status"] is not None and state["approval_status"].status == "rejected":
        return "synthesizer"
    if _requested_tools(state):
        return "tool_agent"
    return "synthesizer"


def _has_ready_rag_call(state: AgentState) -> bool:
    ready_call = state.get("ready_tool_call")
    return ready_call is not None and ready_call.tool_name == "rag_search"


def _has_pending_tool_approval(state: AgentState) -> bool:
    return state["pending_tool_approval"] is not None


def _requested_tools(state: AgentState) -> list[ToolCallRequest]:
    plan = state["plan"] or {}
    return [
        parsed_request
        for request in plan.get("requested_tools", [])
        for parsed_request in [
            request
            if isinstance(request, ToolCallRequest)
            else ToolCallRequest.model_validate(request)
        ]
    ]


def build_question_answer_graph(
    *,
    retriever_node: Callable[[AgentState], AgentState],
    skill_agent_node: Callable[[AgentState], AgentState] | None = None,
    tool_agent_node: Callable[[AgentState], AgentState] | None = None,
    memory_retriever: MemoryRetriever | None = None,
    trace_store: InMemoryTraceStore | None = None,
    checkpointer: Checkpointer = None,
    llm_client: LlmClient | None = None,
    tool_registry_factory: Callable[[], ToolRegistry] | None = None,
):
    runner = NodeRunner(trace_store=trace_store)
    selected_registry_factory = (
        tool_registry_factory
        or getattr(tool_agent_node, "tool_registry_factory", None)
        or get_default_tool_registry
    )
    tool_registry_snapshot = selected_registry_factory().snapshot()
    tool_specs_snapshot = tuple(tool_registry_snapshot.specs())
    selected_skill_agent_node = guard_skill_agent_node(
        skill_agent_node or default_skill_agent_node,
        tool_specs=tool_specs_snapshot,
    )
    selected_tool_agent_node = tool_agent_node or build_tool_agent_node(
        registry_factory=lambda: tool_registry_snapshot
    )
    selected_memory_manager_node = memory_manager_node
    if memory_retriever is not None:
        selected_memory_manager_node = build_memory_manager_node(
            manager=MemoryManager(retrieve_memory=memory_retriever)
        )
    graph = StateGraph(AgentState)
    graph.add_node(
        "memory_manager",
        _with_harness("memory_manager", selected_memory_manager_node, runner),
    )
    graph.add_node(
        "intent_router",
        _with_harness(
            "intent_router",
            build_llm_intent_router_node(llm_client=llm_client),
            runner,
        ),
    )
    graph.add_node(
        "planner_agent",
        _with_harness(
            "planner_agent",
            build_planner_agent_node(
                llm_client=llm_client,
                available_tools=list(tool_specs_snapshot),
            ),
            runner,
        ),
    )
    graph.add_node(
        "skill_agent",
        _with_harness(
            "skill_agent",
            selected_skill_agent_node,
            runner,
        ),
    )
    graph.add_node(
        "tool_agent",
        _with_harness("tool_agent", selected_tool_agent_node, runner),
    )
    graph.add_node(
        "approval_gate",
        _with_harness("approval_gate", approval_gate_node, runner),
    )
    graph.add_node("retriever_agent", _with_harness("retriever_agent", retriever_node, runner))
    graph.add_node("analysis_agent", _with_harness("analysis_agent", analysis_agent_node, runner))
    graph.add_node(
        "synthesizer",
        _with_harness(
            "synthesizer",
            build_synthesizer_node(llm_client=llm_client),
            runner,
        ),
    )
    graph.add_node("verifier", _with_harness("verifier", verifier_node, runner))
    graph.add_node(
        "approval_coordinator",
        _with_harness("approval_coordinator", approval_coordinator_node, runner),
    )
    graph.add_node("final_response", _with_harness("final_response", final_response_node, runner))

    graph.set_entry_point("memory_manager")
    graph.add_edge("memory_manager", "intent_router")
    graph.add_edge("intent_router", "planner_agent")
    graph.add_conditional_edges(
        "planner_agent",
        _route_after_planner,
        {
            "skill_agent": "skill_agent",
            "tool_agent": "tool_agent",
            "approval_gate": "approval_gate",
            "synthesizer": "synthesizer",
        },
    )
    graph.add_conditional_edges(
        "skill_agent",
        _route_after_skill_agent,
        {
            "tool_agent": "tool_agent",
            "approval_gate": "approval_gate",
            "retriever_agent": "retriever_agent",
            "synthesizer": "synthesizer",
        },
    )
    graph.add_conditional_edges(
        "tool_agent",
        _route_after_tool_agent,
        {
            "approval_gate": "approval_gate",
            "retriever_agent": "retriever_agent",
            "synthesizer": "synthesizer",
        },
    )
    graph.add_conditional_edges(
        "approval_gate",
        _route_after_approval_gate,
        {
            "tool_agent": "tool_agent",
            "retriever_agent": "retriever_agent",
            "synthesizer": "synthesizer",
        },
    )
    graph.add_edge("retriever_agent", "analysis_agent")
    graph.add_edge("analysis_agent", "synthesizer")
    graph.add_edge("synthesizer", "verifier")
    graph.add_edge("verifier", "approval_coordinator")
    graph.add_edge("approval_coordinator", "final_response")
    graph.add_edge("final_response", END)
    return graph.compile(checkpointer=checkpointer)


def run_question_answer_graph(
    *,
    session_id: str,
    tenant_id: str,
    user_id: str,
    user_message: str,
    mock_evidence: bool = True,
    retriever_node: Callable[[AgentState], AgentState] | None = None,
    skill_agent_node: Callable[[AgentState], AgentState] | None = None,
    tool_agent_node: Callable[[AgentState], AgentState] | None = None,
    memory_retriever: MemoryRetriever | None = None,
    trace_store: InMemoryTraceStore | None = None,
    checkpointer: Checkpointer = None,
    llm_client: LlmClient | None = None,
    permission_scopes: list[str] | None = None,
    tool_registry_factory: Callable[[], ToolRegistry] | None = None,
) -> AgentState:
    state = create_initial_state(
        session_id=session_id,
        tenant_id=tenant_id,
        user_id=user_id,
        user_message=user_message,
        permission_scopes=permission_scopes,
    )
    selected_retriever_node = retriever_node
    if selected_retriever_node is None:
        selected_retriever_node = (
            _mock_retrieval_node if mock_evidence else build_default_rag_retriever_node()
        )
    graph = build_question_answer_graph(
        retriever_node=selected_retriever_node,
        skill_agent_node=skill_agent_node,
        tool_agent_node=tool_agent_node,
        memory_retriever=memory_retriever,
        trace_store=trace_store,
        checkpointer=checkpointer,
        llm_client=llm_client,
        tool_registry_factory=tool_registry_factory,
    )
    config = (
        {"configurable": {"thread_id": state["request_id"]}}
        if checkpointer is not None
        else None
    )
    return graph.invoke(state, config=config)


def resume_question_answer_graph(
    *,
    checkpoint_thread_id: str,
    approval_decision: dict,
    mock_evidence: bool = True,
    retriever_node: Callable[[AgentState], AgentState] | None = None,
    skill_agent_node: Callable[[AgentState], AgentState] | None = None,
    tool_agent_node: Callable[[AgentState], AgentState] | None = None,
    memory_retriever: MemoryRetriever | None = None,
    trace_store: InMemoryTraceStore | None = None,
    checkpointer: Checkpointer,
    llm_client: LlmClient | None = None,
    permission_scopes: list[str] | None = None,
    tool_registry_factory: Callable[[], ToolRegistry] | None = None,
) -> AgentState:
    selected_retriever_node = retriever_node
    if selected_retriever_node is None:
        selected_retriever_node = (
            _mock_retrieval_node if mock_evidence else build_default_rag_retriever_node()
        )
    graph = build_question_answer_graph(
        retriever_node=selected_retriever_node,
        skill_agent_node=skill_agent_node,
        tool_agent_node=tool_agent_node,
        memory_retriever=memory_retriever,
        trace_store=trace_store,
        checkpointer=checkpointer,
        llm_client=llm_client,
        tool_registry_factory=tool_registry_factory,
    )
    command = Command(
        resume=approval_decision,
        update={"permission_scopes": list(permission_scopes or [])},
    )
    return graph.invoke(
        command,
        config={"configurable": {"thread_id": checkpoint_thread_id}},
    )
