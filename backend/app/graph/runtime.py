from collections.abc import Callable, Iterator
from typing import Any

from langgraph.graph import END, StateGraph
from langgraph.types import Checkpointer, Command, RetryPolicy

from app.graph.nodes.analysis_agent import analysis_agent_node
from app.graph.nodes.attachment_analyzer import (
    AttachmentContextResolver,
    build_attachment_analyzer_node,
)
from app.graph.nodes.approval_gate import build_approval_gate_node
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
from app.schemas.messages import Message
from app.harness.error_boundary import (
    build_safe_error_record,
    build_node_error_handler,
    classify_recoverable_error,
    is_terminal_recoverable_code,
    is_retryable_recoverable_error,
    should_route_recoverable_error_to_synthesizer,
)
from app.harness.node_runner import NodeRunner
from app.harness.observer import NodeObserver
from app.harness.tracing import InMemoryTraceStore
from app.llm.factory import LlmClient
from app.schemas.tools import ToolCallRequest
from app.tools.default_registry import get_default_tool_registry
from app.tools.registry import ToolCatalogChangedError, ToolRegistry


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


class _UnavailableAttachmentResolver:
    def resolve(self, *_args, **_kwargs):
        error = RuntimeError("Attachment resolver is required for image messages.")
        error.code = "attachment_resolver_unavailable"
        raise error



def _catalog_guard_node(*, catalog_digest: str):
    def node(state: AgentState) -> AgentState:
        existing = state.get("tool_catalog_digest")
        if existing and existing != catalog_digest:
            raise ToolCatalogChangedError()
        state["tool_catalog_digest"] = catalog_digest
        return state

    return node


def final_response_node(state: AgentState) -> AgentState:
    draft = state["draft_answer"]
    approval_status = state["approval_status"]
    state["final_response"] = {
        "content": draft["content"] if draft else "没有生成答案。",
        "trace_id": state["trace_id"],
        "cited_evidence_ids": draft.get("cited_evidence_ids", []) if draft else [],
        "approval_status": approval_status.status if approval_status else "not_required",
    }
    if draft and draft.get("response_type"):
        state["final_response"]["response_type"] = draft["response_type"]
    if draft and draft.get("business_error_code"):
        state["final_response"]["business_error_code"] = draft[
            "business_error_code"
        ]
    _append_assistant_message(state)
    return state


def _append_assistant_message(state: AgentState) -> None:
    final_response = state["final_response"] or {}
    content = final_response.get("content")
    if not content:
        return
    message_id = state["assistant_message_id"]
    if any(
        message.message_id == message_id
        for message in state["messages"]
    ):
        return
    state["messages"].append(
        Message(
            message_id=message_id,
            role="assistant",
            content=content,
            metadata={
                "request_id": state["request_id"],
                "trace_id": state["trace_id"],
            },
        )
    )
    state["conversation_history"] = list(state["messages"])


def _with_harness(
    node_name: str,
    node: Callable[[AgentState], AgentState],
    runner: NodeRunner,
) -> Callable[[AgentState], AgentState]:
    def wrapped(state: AgentState) -> AgentState:
        previous_error_count = len(state["errors"])
        try:
            result = runner.run(node_name, node, state, handle_recoverable_errors=False)
            for error in result["errors"][previous_error_count:]:
                if is_terminal_recoverable_code(error.get("code")):
                    raise AgentGraphExecutionError(
                        node_name=error["node"],
                        message=error["message"],
                        code=error.get("code", "agent_graph_execution_failed"),
                        http_status=error.get("http_status", 500),
                    )
            return result
        except Exception as exc:
            classified = classify_recoverable_error(exc)
            if classified is not None and should_route_recoverable_error_to_synthesizer(
                exc
            ):
                raise
            if classified is not None:
                error_record = build_safe_error_record(node_name, exc)
                graph_error = AgentGraphExecutionError(
                    node_name=node_name,
                    message=error_record["message"],
                    code=error_record.get("code", "agent_graph_execution_failed"),
                    http_status=error_record.get("http_status", 500),
                )
                graph_error.trace_recorded = True
                raise graph_error from exc
            if isinstance(exc, AgentGraphExecutionError):
                raise
            graph_error = AgentGraphExecutionError(
                node_name=node_name,
                message="节点执行失败。",
                code="agent_node_error",
                http_status=500,
            )
            graph_error.trace_recorded = True
            raise graph_error from exc

    return wrapped


_OPERATIONAL_RETRY_NODE_NAMES = frozenset(
    {
        "attachment_analyzer",
        "retriever_agent",
        "skill_agent",
    }
)

_OPERATIONAL_RETRY_POLICY = RetryPolicy(
    initial_interval=0.2,
    backoff_factor=2.0,
    max_interval=2.0,
    max_attempts=3,
    retry_on=is_retryable_recoverable_error,
)


def _node_retry_policy(node_name: str) -> RetryPolicy | None:
    if node_name in _OPERATIONAL_RETRY_NODE_NAMES:
        return _OPERATIONAL_RETRY_POLICY
    return None


def _add_harnessed_node(
    graph: StateGraph,
    node_name: str,
    node: Callable[[AgentState], AgentState],
    runner: NodeRunner,
) -> None:
    handled_goto = None if node_name in {"synthesizer", "final_response"} else "synthesizer"
    graph.add_node(
        node_name,
        _with_harness(node_name, node, runner),
        error_handler=build_node_error_handler(
            node_name,
            observer=runner.observer,
            handled_goto=handled_goto,
        ),
        retry_policy=_node_retry_policy(node_name),
        destinations=(handled_goto,) if handled_goto is not None else None,
    )


def _route_after_planner(state: AgentState) -> str:
    plan = state["plan"] or {}
    if plan.get("execution_mode") == "image_analysis":
        return "analysis_agent"
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
    if state["image_analyses"]:
        return "analysis_agent"
    return "synthesizer"


def _route_after_tool_agent(state: AgentState) -> str:
    if _has_pending_tool_approval(state):
        return "approval_gate"
    if _has_ready_rag_call(state):
        return "retriever_agent"
    plan = state["plan"] or {}
    if (
        plan.get("execution_mode") == "skill_execution"
        or state["image_analyses"]
        or _has_stock_tool_result(state)
    ):
        return "analysis_agent"
    return "synthesizer"


def _route_after_approval_gate(state: AgentState) -> str:
    if state["approval_status"] is not None and state["approval_status"].status == "rejected":
        return "synthesizer"
    if _requested_tools(state):
        return "tool_agent"
    if state["image_analyses"]:
        return "analysis_agent"
    return "synthesizer"


def _has_ready_rag_call(state: AgentState) -> bool:
    ready_call = state.get("ready_tool_call")
    return ready_call is not None and ready_call.tool_name == "rag_search"


def _has_stock_tool_result(state: AgentState) -> bool:
    return any(
        ".stock." in record.request.tool_name and record.result is not None
        for record in state["tool_calls"]
    )


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


def _runtime_registry_snapshot(
    *,
    registry_factory: Callable[[], ToolRegistry],
    retriever_node: Callable[[AgentState], AgentState],
) -> ToolRegistry:
    registry = registry_factory().mutable_copy()
    for tool, execution_binding in getattr(
        retriever_node,
        "tool_registry_contributions",
        (),
    ):
        registry.register(tool, execution_binding=execution_binding)
    return registry.snapshot()


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
    tool_registry_snapshot: ToolRegistry | None = None,
    allow_test_tool_agent_override: bool = False,
    attachment_resolver: AttachmentContextResolver | None = None,
):
    runner = NodeRunner(trace_store=trace_store)
    selected_registry_factory = tool_registry_factory or getattr(
        tool_agent_node, "tool_registry_factory", None
    ) or get_default_tool_registry
    tool_registry_snapshot = tool_registry_snapshot or _runtime_registry_snapshot(
        registry_factory=selected_registry_factory,
        retriever_node=retriever_node,
    )
    tool_specs_snapshot = tuple(tool_registry_snapshot.specs())
    selected_skill_agent_node = guard_skill_agent_node(
        skill_agent_node or default_skill_agent_node,
        tool_specs=tool_specs_snapshot,
    )
    if tool_agent_node is not None and not getattr(
        tool_agent_node, "is_trusted_tool_agent", False
    ):
        if not allow_test_tool_agent_override:
            raise ValueError("任意 ToolAgent callable 仅允许通过显式测试钩子注入。")
        selected_tool_agent_node = tool_agent_node
    else:
        selected_tool_agent_node = build_tool_agent_node(
            registry_factory=lambda: tool_registry_snapshot,
            llm_client=llm_client,
        )
    selected_memory_manager_node = memory_manager_node
    if memory_retriever is not None:
        selected_memory_manager_node = build_memory_manager_node(
            manager=MemoryManager(retrieve_memory=memory_retriever)
        )
    graph = StateGraph(AgentState)
    _add_harnessed_node(
        graph,
        "catalog_guard",
        _catalog_guard_node(catalog_digest=tool_registry_snapshot.catalog_digest),
        runner,
    )
    _add_harnessed_node(
        graph,
        "attachment_analyzer",
        build_attachment_analyzer_node(
            resolver=attachment_resolver or _UnavailableAttachmentResolver()
        ),
        runner,
    )
    _add_harnessed_node(
        graph,
        "memory_manager",
        selected_memory_manager_node,
        runner,
    )
    _add_harnessed_node(
        graph,
        "intent_router",
        build_llm_intent_router_node(llm_client=llm_client),
        runner,
    )
    _add_harnessed_node(
        graph,
        "planner_agent",
        build_planner_agent_node(
            llm_client=llm_client,
            available_tools=list(tool_specs_snapshot),
        ),
        runner,
    )
    _add_harnessed_node(
        graph,
        "skill_agent",
        selected_skill_agent_node,
        runner,
    )
    _add_harnessed_node(
        graph,
        "tool_agent",
        selected_tool_agent_node,
        runner,
    )
    _add_harnessed_node(
        graph,
        "approval_gate",
        build_approval_gate_node(
            tool_catalog_digest=tool_registry_snapshot.catalog_digest
        ),
        runner,
    )
    _add_harnessed_node(graph, "retriever_agent", retriever_node, runner)
    _add_harnessed_node(graph, "analysis_agent", analysis_agent_node, runner)
    _add_harnessed_node(
        graph,
        "synthesizer",
        build_synthesizer_node(llm_client=llm_client),
        runner,
    )
    _add_harnessed_node(graph, "verifier", verifier_node, runner)
    _add_harnessed_node(
        graph,
        "approval_coordinator",
        approval_coordinator_node,
        runner,
    )
    _add_harnessed_node(graph, "final_response", final_response_node, runner)

    graph.set_entry_point("catalog_guard")
    graph.add_edge("catalog_guard", "attachment_analyzer")
    graph.add_edge("attachment_analyzer", "memory_manager")
    graph.add_edge("memory_manager", "intent_router")
    graph.add_edge("intent_router", "planner_agent")
    graph.add_conditional_edges(
        "planner_agent",
        _route_after_planner,
        {
            "analysis_agent": "analysis_agent",
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
            "analysis_agent": "analysis_agent",
            "synthesizer": "synthesizer",
        },
    )
    graph.add_conditional_edges(
        "tool_agent",
        _route_after_tool_agent,
        {
            "approval_gate": "approval_gate",
            "retriever_agent": "retriever_agent",
            "analysis_agent": "analysis_agent",
            "synthesizer": "synthesizer",
        },
    )
    graph.add_conditional_edges(
        "approval_gate",
        _route_after_approval_gate,
        {
            "tool_agent": "tool_agent",
            "retriever_agent": "retriever_agent",
            "analysis_agent": "analysis_agent",
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
    session_key: str | None = None,
    tenant_id: str,
    user_id: str,
    user_message: str,
    request_id: str | None = None,
    trace_id: str | None = None,
    user_message_id: str | None = None,
    assistant_message_id: str | None = None,
    history: list[Message] | None = None,
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
    allow_test_tool_agent_override: bool = False,
    attachment_ids: list[str] | None = None,
    attachment_resolver: AttachmentContextResolver | None = None,
) -> AgentState:
    state, graph, config = _prepare_question_answer_run(
        session_id=session_id,
        session_key=session_key,
        tenant_id=tenant_id,
        user_id=user_id,
        user_message=user_message,
        request_id=request_id,
        trace_id=trace_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        history=history,
        mock_evidence=mock_evidence,
        retriever_node=retriever_node,
        skill_agent_node=skill_agent_node,
        tool_agent_node=tool_agent_node,
        memory_retriever=memory_retriever,
        trace_store=trace_store,
        checkpointer=checkpointer,
        llm_client=llm_client,
        permission_scopes=permission_scopes,
        tool_registry_factory=tool_registry_factory,
        allow_test_tool_agent_override=allow_test_tool_agent_override,
        attachment_ids=attachment_ids,
        attachment_resolver=attachment_resolver,
    )
    return graph.invoke(state, config=config)


def stream_question_answer_graph(
    *,
    session_id: str,
    session_key: str | None = None,
    tenant_id: str,
    user_id: str,
    user_message: str,
    request_id: str | None = None,
    trace_id: str | None = None,
    user_message_id: str | None = None,
    assistant_message_id: str | None = None,
    history: list[Message] | None = None,
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
    allow_test_tool_agent_override: bool = False,
    attachment_ids: list[str] | None = None,
    attachment_resolver: AttachmentContextResolver | None = None,
) -> Iterator[AgentState]:
    state, graph, config = _prepare_question_answer_run(
        session_id=session_id,
        session_key=session_key,
        tenant_id=tenant_id,
        user_id=user_id,
        user_message=user_message,
        request_id=request_id,
        trace_id=trace_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        history=history,
        mock_evidence=mock_evidence,
        retriever_node=retriever_node,
        skill_agent_node=skill_agent_node,
        tool_agent_node=tool_agent_node,
        memory_retriever=memory_retriever,
        trace_store=trace_store,
        checkpointer=checkpointer,
        llm_client=llm_client,
        permission_scopes=permission_scopes,
        tool_registry_factory=tool_registry_factory,
        allow_test_tool_agent_override=allow_test_tool_agent_override,
        attachment_ids=attachment_ids,
        attachment_resolver=attachment_resolver,
    )
    latest_state = state
    for update in graph.stream(state, config=config, stream_mode="updates"):
        next_state = _state_from_stream_update(update, fallback=latest_state)
        if next_state is not None:
            latest_state = next_state
            yield latest_state


def _prepare_question_answer_run(
    *,
    session_id: str,
    session_key: str | None,
    tenant_id: str,
    user_id: str,
    user_message: str,
    request_id: str | None,
    trace_id: str | None,
    user_message_id: str | None,
    assistant_message_id: str | None,
    history: list[Message] | None,
    mock_evidence: bool,
    retriever_node: Callable[[AgentState], AgentState] | None,
    skill_agent_node: Callable[[AgentState], AgentState] | None,
    tool_agent_node: Callable[[AgentState], AgentState] | None,
    memory_retriever: MemoryRetriever | None,
    trace_store: InMemoryTraceStore | None,
    checkpointer: Checkpointer,
    llm_client: LlmClient | None,
    permission_scopes: list[str] | None,
    tool_registry_factory: Callable[[], ToolRegistry] | None,
    allow_test_tool_agent_override: bool,
    attachment_ids: list[str] | None,
    attachment_resolver: AttachmentContextResolver | None,
) -> tuple[AgentState, Any, dict | None]:
    selected_retriever_node = retriever_node
    if selected_retriever_node is None:
        selected_retriever_node =  build_default_rag_retriever_node()
    selected_registry_factory = tool_registry_factory or getattr(
        tool_agent_node, "tool_registry_factory", None
    ) or get_default_tool_registry
    tool_registry_snapshot = _runtime_registry_snapshot(
        registry_factory=selected_registry_factory,
        retriever_node=selected_retriever_node,
    )
    state = create_initial_state(
        session_id=session_id,
        session_key=session_key,
        tenant_id=tenant_id,
        user_id=user_id,
        user_message=user_message,
        request_id=request_id,
        trace_id=trace_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        history=history,
        permission_scopes=permission_scopes,
        tool_catalog_digest=tool_registry_snapshot.catalog_digest,
        attachment_ids=attachment_ids,
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
        tool_registry_snapshot=tool_registry_snapshot,
        allow_test_tool_agent_override=allow_test_tool_agent_override,
        attachment_resolver=attachment_resolver,
    )
    config = _checkpoint_config(state, enabled=checkpointer is not None)
    return state, graph, config


def _checkpoint_config(state: AgentState | dict, *, enabled: bool) -> dict | None:
    if not enabled:
        return None
    return {
        "configurable": {
            "thread_id": state["session_key"],
            "checkpoint_ns": state["tenant_id"],
        }
    }


def _state_from_stream_update(
    update: dict[str, Any],
    *,
    fallback: AgentState,
) -> AgentState | None:
    interrupt_payload = update.get("__interrupt__")
    if interrupt_payload:
        interrupted_state = dict(fallback)
        interrupted_state["__interrupt__"] = list(interrupt_payload)
        return interrupted_state
    for node_update in update.values():
        if isinstance(node_update, dict) and "request_id" in node_update:
            return node_update
    return None


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
    allow_test_tool_agent_override: bool = False,
) -> AgentState:
    selected_retriever_node = retriever_node
    if selected_retriever_node is None:
        selected_retriever_node = build_default_rag_retriever_node()
    selected_registry_factory = tool_registry_factory or getattr(
        tool_agent_node, "tool_registry_factory", None
    ) or get_default_tool_registry
    tool_registry_snapshot = _runtime_registry_snapshot(
        registry_factory=selected_registry_factory,
        retriever_node=selected_retriever_node,
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
        tool_registry_snapshot=tool_registry_snapshot,
        allow_test_tool_agent_override=allow_test_tool_agent_override,
    )
    config = {"configurable": {"thread_id": checkpoint_thread_id}}
    snapshot = graph.get_state(config)
    checkpoint_digest = snapshot.values.get("tool_catalog_digest")
    if snapshot.values and checkpoint_digest != tool_registry_snapshot.catalog_digest:
        _record_resume_catalog_guard_trace(
            trace_store=trace_store,
            state=snapshot.values,
            error=ToolCatalogChangedError(),
        )
        raise AgentGraphExecutionError(
            node_name="catalog_guard",
            message="工具目录已变化，不能继续审批恢复。",
            code="tool_catalog_changed",
            http_status=409,
        )
    command = Command(
        resume=approval_decision,
        update=(
            {"permission_scopes": list(permission_scopes)}
            if permission_scopes is not None
            else None
        ),
    )
    return graph.invoke(
        command,
        config=config,
    )


def _record_resume_catalog_guard_trace(
    *,
    trace_store: InMemoryTraceStore | None,
    state: AgentState,
    error: BaseException,
) -> None:
    observer = NodeObserver(trace_store=trace_store)
    observation = observer.observe_start("catalog_guard", state)
    observer.record_error(
        observation=observation,
        state=state,
        error=error,
        error_code=getattr(error, "code", "agent_node_error"),
    )
