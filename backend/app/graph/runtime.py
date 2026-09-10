from collections.abc import Callable, Iterator
import re
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer, Command, RetryPolicy, Send

from app.core.config import settings
from app.graph.nodes.analysis_agent import build_analysis_agent_node
from app.graph.nodes.attachment_analyzer import (
    AttachmentContextResolver,
    build_attachment_analyzer_node,
)
from app.graph.nodes.approval_gate import (
    ApprovalPayloadMismatchError,
    build_approval_gate_node,
)
from app.graph.nodes.approval_coordinator import approval_coordinator_node
from app.graph.nodes.intent_router import build_intent_router_node
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.nodes.skill_agent import build_skill_agent_node
from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.nodes.verifier import verifier_node
from app.graph.state import (
    AgentState,
    build_checkpoint_thread_id,
    create_initial_state,
)
from app.graph.planning.contracts import PlanModel, SubTaskModel, TaskResultModel
from app.graph.planning.scheduler import plan_status, ready_tasks
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
from app.harness.trace_buffer import TraceBuffer
from app.harness.tracing import InMemoryTraceStore, TraceEvent
from app.intent.catalog import BusinessIntentCatalog
from app.llm.factory import LlmClient
from app.schemas.messages import Message
from app.skills.factory import build_default_skill_registry
from app.skills.registry import SkillRegistry
from app.tools.default_registry import (
    get_default_tool_catalog_runtime,
    get_default_tool_registry,
)
from app.tools.registry import ToolCatalogChangedError, ToolRegistry
from app.tools.search import ToolSearchService


_DEFAULT_TOOL_REGISTRY_FACTORY = get_default_tool_registry


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


class LegacyCheckpointIncompatibleError(RuntimeError):
    code = "legacy_checkpoint_incompatible"
    http_status = 409

    def __init__(self) -> None:
        super().__init__(
            "旧 Checkpoint 不可安全恢复：坐标缺少可靠的租户或 Run 归属，请重新执行请求。"
        )


_CHECKPOINT_THREAD_ID_PATTERN = re.compile(r"cpr1_[0-9a-f]{64}")


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


def _memory_recall_failure_trace_callback(
    *,
    trace_buffer: TraceBuffer | None,
    trace_store: InMemoryTraceStore | None,
) -> Callable[[AgentState], None]:
    def callback(state: AgentState) -> None:
        event = TraceEvent(
            trace_id=state["trace_id"],
            node_name="experience_memory",
            status="handled_error",
            latency_ms=0,
            error="experience_memory_recall_failed",
            metadata={"event": "experience_memory_recall_failed"},
        )
        if trace_buffer is not None:
            trace_buffer.record_event(event)
        if trace_store is not None:
            trace_store.record_event(event)

    return callback


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
        message.id == message_id
        for message in state["messages"]
    ):
        return
    state["messages"] = add_messages(
        state["messages"],
        [
            AIMessage(
                id=message_id,
                content=content,
                additional_kwargs={
                    "metadata": {
                        "request_id": state["request_id"],
                        "trace_id": state["trace_id"],
                    }
                },
            )
        ],
    )


def _with_harness(
    node_name: str,
    node: Callable[..., dict[str, Any]],
    runner: NodeRunner,
    *,
    runtime_aware: bool = False,
) -> Callable[..., dict[str, Any]]:
    def execute(state: AgentState, runtime: Any | None = None) -> dict[str, Any]:
        previous_error_count = len(state["errors"])
        try:
            result = runner.run(
                node_name,
                node,
                state,
                runtime=runtime,
                runtime_aware=runtime_aware,
                handle_recoverable_errors=False,
            )
            result_errors = result.get("errors", [])
            start_index = previous_error_count if result_errors is state["errors"] else 0
            for error in result_errors[start_index:]:
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

    if runtime_aware:
        def wrapped(state: AgentState, runtime: Any) -> dict[str, Any]:
            return execute(state, runtime)

        return wrapped

    def wrapped(state: AgentState) -> dict[str, Any]:
        return execute(state)

    return wrapped


_OPERATIONAL_RETRY_NODE_NAMES = frozenset(
    {
        "attachment_analyzer",
        "skill_agent",
        "tool_agent",
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
    node: Callable[..., AgentState],
    runner: NodeRunner,
    *,
    runtime_aware: bool = False,
) -> None:
    handled_goto = None if node_name in {"synthesizer", "final_response"} else "synthesizer"
    graph.add_node(
        node_name,
        _with_harness(node_name, node, runner, runtime_aware=runtime_aware),
        error_handler=build_node_error_handler(
            node_name,
            observer=runner.observer,
            handled_goto=handled_goto,
        ),
        retry_policy=_node_retry_policy(node_name),
        destinations=(handled_goto,) if handled_goto is not None else None,
    )


def _route_after_intent(state: AgentState) -> str:
    intent = state.get("intent") or {}
    return "planner_agent" if intent.get("intent_type") else "synthesizer"


def task_scheduler_node(_state: AgentState) -> dict[str, Any]:
    return {}


def _dispatch_after_scheduler(state: AgentState) -> str | list[Send]:
    if _has_pending_memory_approval(state) or _has_pending_tool_approval(state):
        return "approval_gate"
    plan = PlanModel.model_validate(state.get("plan"))
    results = [
        TaskResultModel.model_validate(item)
        for item in state.get("task_results", [])
    ]
    status = plan_status(plan, results=results)
    if _completed_tool_search(plan, results=results):
        if state["planner_iteration"] >= settings.planner_max_iterations:
            return "synthesizer"
        return "planner_agent"
    if status in {"complete", "terminal_failure"}:
        return "synthesizer"
    if status == "needs_replan":
        if state["planner_iteration"] >= settings.planner_max_iterations:
            return "synthesizer"
        return "planner_agent"
    return [
        Send(
            _worker_name(task),
            {**state, "active_task": task.model_dump(mode="python")},
        )
        for task in ready_tasks(plan, results=results)
    ]


def _worker_name(task: SubTaskModel) -> str:
    if task.skill_name is not None:
        return "skill_agent"
    if task.tool_name is not None:
        return "tool_agent"
    return "analysis_agent"


def _completed_tool_search(
    plan: PlanModel,
    *,
    results: list[TaskResultModel],
) -> bool:
    successful_ids = {
        result.task_id for result in results if result.status == "success"
    }
    return any(
        task.tool_name == "search_tool" and task.id in successful_ids
        for task in plan.tasks
    )


def _route_after_approval_gate(state: AgentState) -> str:
    if state["approval_status"] is not None and state["approval_status"].status == "rejected":
        return "synthesizer"
    return "task_scheduler"


def _has_pending_tool_approval(state: AgentState) -> bool:
    return state["pending_tool_approval"] is not None


def _has_pending_memory_approval(state: AgentState) -> bool:
    return any(
        candidate.approval_status == "pending"
        and candidate.sensitivity in {"medium", "high"}
        for candidate in state["memory_candidates"]
    )


def _runtime_registry_snapshot(
    *,
    registry_factory: Callable[[], ToolRegistry],
) -> ToolRegistry:
    return registry_factory().mutable_copy().snapshot()


def _resolve_tool_search_service(
    *,
    explicit_service: ToolSearchService | None,
    registry_factory: Callable[[], ToolRegistry],
    registry_snapshot: ToolRegistry,
) -> ToolSearchService | None:
    service = explicit_service
    if service is None and registry_factory is _DEFAULT_TOOL_REGISTRY_FACTORY:
        service = get_default_tool_catalog_runtime().search_service
    if service is not None and service.catalog_digest != registry_snapshot.catalog_digest:
        raise ValueError("工具搜索服务与 Graph 工具目录 digest 不一致。")
    return service


def build_question_answer_graph(
    *,
    business_intent_catalog: BusinessIntentCatalog,
    skill_agent_node: Callable[[AgentState], AgentState] | None = None,
    tool_agent_node: Callable[[AgentState], AgentState] | None = None,
    store: BaseStore | None = None,
    trace_buffer: TraceBuffer | None = None,
    trace_store: InMemoryTraceStore | None = None,
    checkpointer: Checkpointer = None,
    llm_client: LlmClient | None = None,
    tool_registry_factory: Callable[[], ToolRegistry] | None = None,
    tool_registry_snapshot: ToolRegistry | None = None,
    tool_search_service: ToolSearchService | None = None,
    allow_test_tool_agent_override: bool = False,
    attachment_resolver: AttachmentContextResolver | None = None,
):
    runner = NodeRunner(trace_buffer=trace_buffer, trace_store=trace_store)
    selected_registry_factory = tool_registry_factory or getattr(
        tool_agent_node, "tool_registry_factory", None
    ) or get_default_tool_registry
    tool_registry_snapshot = tool_registry_snapshot or _runtime_registry_snapshot(
        registry_factory=selected_registry_factory,
    )
    selected_tool_search_service = _resolve_tool_search_service(
        explicit_service=tool_search_service,
        registry_factory=selected_registry_factory,
        registry_snapshot=tool_registry_snapshot,
    )
    selected_skill_registry = (
        build_default_skill_registry()
        if skill_agent_node is None
        else SkillRegistry()
    )
    selected_skill_agent_node = skill_agent_node or build_skill_agent_node(
        registry=selected_skill_registry,
        llm_client=llm_client,
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
    graph = StateGraph(AgentState)
    memory_recall_failure_trace = _memory_recall_failure_trace_callback(
        trace_buffer=trace_buffer,
        trace_store=trace_store,
    )
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
        "intent_router",
        build_intent_router_node(
            catalog=business_intent_catalog,
            llm_client=llm_client,
        ),
        runner,
    )
    _add_harnessed_node(
        graph,
        "planner_agent",
        build_planner_agent_node(
            llm_client=llm_client,
            tool_search_service=selected_tool_search_service,
            skill_registry=selected_skill_registry,
            on_memory_recall_failure=memory_recall_failure_trace,
        ),
        runner,
        runtime_aware=True,
    )
    _add_harnessed_node(graph, "task_scheduler", task_scheduler_node, runner)
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
    _add_harnessed_node(
        graph,
        "analysis_agent",
        build_analysis_agent_node(llm_client=llm_client),
        runner,
    )
    _add_harnessed_node(
        graph,
        "synthesizer",
        build_synthesizer_node(
            llm_client=llm_client,
            on_memory_recall_failure=memory_recall_failure_trace,
        ),
        runner,
        runtime_aware=True,
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
    graph.add_edge("attachment_analyzer", "intent_router")
    graph.add_conditional_edges(
        "intent_router",
        _route_after_intent,
        {
            "planner_agent": "planner_agent",
            "synthesizer": "synthesizer",
        },
    )
    graph.add_edge("planner_agent", "task_scheduler")
    graph.add_conditional_edges(
        "task_scheduler",
        _dispatch_after_scheduler,
        {
            "planner_agent": "planner_agent",
            "approval_gate": "approval_gate",
            "synthesizer": "synthesizer",
        },
    )
    graph.add_edge("skill_agent", "task_scheduler")
    graph.add_edge("tool_agent", "task_scheduler")
    graph.add_edge("analysis_agent", "task_scheduler")
    graph.add_conditional_edges(
        "approval_gate",
        _route_after_approval_gate,
        {
            "task_scheduler": "task_scheduler",
            "synthesizer": "synthesizer",
        },
    )
    graph.add_edge("synthesizer", "verifier")
    graph.add_edge("verifier", "approval_coordinator")
    graph.add_edge("approval_coordinator", "final_response")
    graph.add_edge("final_response", END)
    return graph.compile(checkpointer=checkpointer, store=store)


def run_question_answer_graph(
    *,
    business_intent_catalog: BusinessIntentCatalog,
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
    skill_agent_node: Callable[[AgentState], AgentState] | None = None,
    tool_agent_node: Callable[[AgentState], AgentState] | None = None,
    store: BaseStore | None = None,
    trace_buffer: TraceBuffer | None = None,
    trace_store: InMemoryTraceStore | None = None,
    checkpointer: Checkpointer = None,
    llm_client: LlmClient | None = None,
    permission_scopes: list[str] | None = None,
    tool_registry_factory: Callable[[], ToolRegistry] | None = None,
    tool_search_service: ToolSearchService | None = None,
    allow_test_tool_agent_override: bool = False,
    attachment_ids: list[str] | None = None,
    attachment_resolver: AttachmentContextResolver | None = None,
) -> AgentState:
    state, graph, config = _prepare_question_answer_run(
        session_id=session_id,
        business_intent_catalog=business_intent_catalog,
        session_key=session_key,
        tenant_id=tenant_id,
        user_id=user_id,
        user_message=user_message,
        request_id=request_id,
        trace_id=trace_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        history=history,
        skill_agent_node=skill_agent_node,
        tool_agent_node=tool_agent_node,
        store=store,
        trace_buffer=trace_buffer,
        trace_store=trace_store,
        checkpointer=checkpointer,
        llm_client=llm_client,
        permission_scopes=permission_scopes,
        tool_registry_factory=tool_registry_factory,
        tool_search_service=tool_search_service,
        allow_test_tool_agent_override=allow_test_tool_agent_override,
        attachment_ids=attachment_ids,
        attachment_resolver=attachment_resolver,
    )
    return graph.invoke(state, config=config)


def stream_question_answer_graph(
    *,
    business_intent_catalog: BusinessIntentCatalog,
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
    skill_agent_node: Callable[[AgentState], AgentState] | None = None,
    tool_agent_node: Callable[[AgentState], AgentState] | None = None,
    store: BaseStore | None = None,
    trace_buffer: TraceBuffer | None = None,
    trace_store: InMemoryTraceStore | None = None,
    checkpointer: Checkpointer = None,
    llm_client: LlmClient | None = None,
    permission_scopes: list[str] | None = None,
    tool_registry_factory: Callable[[], ToolRegistry] | None = None,
    tool_search_service: ToolSearchService | None = None,
    allow_test_tool_agent_override: bool = False,
    attachment_ids: list[str] | None = None,
    attachment_resolver: AttachmentContextResolver | None = None,
) -> Iterator[AgentState]:
    state, graph, config = _prepare_question_answer_run(
        session_id=session_id,
        business_intent_catalog=business_intent_catalog,
        session_key=session_key,
        tenant_id=tenant_id,
        user_id=user_id,
        user_message=user_message,
        request_id=request_id,
        trace_id=trace_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        history=history,
        skill_agent_node=skill_agent_node,
        tool_agent_node=tool_agent_node,
        store=store,
        trace_buffer=trace_buffer,
        trace_store=trace_store,
        checkpointer=checkpointer,
        llm_client=llm_client,
        permission_scopes=permission_scopes,
        tool_registry_factory=tool_registry_factory,
        tool_search_service=tool_search_service,
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
    business_intent_catalog: BusinessIntentCatalog,
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
    skill_agent_node: Callable[[AgentState], AgentState] | None,
    tool_agent_node: Callable[[AgentState], AgentState] | None,
    store: BaseStore | None,
    trace_buffer: TraceBuffer | None,
    trace_store: InMemoryTraceStore | None,
    checkpointer: Checkpointer,
    llm_client: LlmClient | None,
    permission_scopes: list[str] | None,
    tool_registry_factory: Callable[[], ToolRegistry] | None,
    tool_search_service: ToolSearchService | None,
    allow_test_tool_agent_override: bool,
    attachment_ids: list[str] | None,
    attachment_resolver: AttachmentContextResolver | None,
) -> tuple[AgentState, Any, dict | None]:
    selected_registry_factory = tool_registry_factory or getattr(
        tool_agent_node, "tool_registry_factory", None
    ) or get_default_tool_registry
    tool_registry_snapshot = _runtime_registry_snapshot(
        registry_factory=selected_registry_factory,
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
        business_intent_catalog=business_intent_catalog,
        skill_agent_node=skill_agent_node,
        tool_agent_node=tool_agent_node,
        store=store,
        trace_buffer=trace_buffer,
        trace_store=trace_store,
        checkpointer=checkpointer,
        llm_client=llm_client,
        tool_registry_factory=tool_registry_factory,
        tool_registry_snapshot=tool_registry_snapshot,
        tool_search_service=tool_search_service,
        allow_test_tool_agent_override=allow_test_tool_agent_override,
        attachment_resolver=attachment_resolver,
    )
    config = _checkpoint_config(state, enabled=checkpointer is not None)
    return state, graph, config


def _checkpoint_config(state: AgentState | dict, *, enabled: bool) -> dict:
    config: dict[str, Any] = {"recursion_limit": settings.graph_recursion_limit}
    if not enabled:
        return config
    config["configurable"] = {
        "thread_id": build_checkpoint_thread_id(
            tenant_id=state["tenant_id"],
            session_key=state["session_key"],
            request_id=state["request_id"],
        ),
        "checkpoint_ns": "",
    }
    return config


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
    business_intent_catalog: BusinessIntentCatalog,
    checkpoint_thread_id: str,
    checkpoint_namespace: str = "",
    checkpoint_id: str,
    interrupt_id: str,
    action_digest: str,
    approval_decision: dict,
    skill_agent_node: Callable[[AgentState], AgentState] | None = None,
    tool_agent_node: Callable[[AgentState], AgentState] | None = None,
    store: BaseStore | None = None,
    trace_buffer: TraceBuffer | None = None,
    trace_store: InMemoryTraceStore | None = None,
    checkpointer: Checkpointer,
    llm_client: LlmClient | None = None,
    permission_scopes: list[str] | None = None,
    tool_registry_factory: Callable[[], ToolRegistry] | None = None,
    tool_search_service: ToolSearchService | None = None,
    allow_test_tool_agent_override: bool = False,
) -> AgentState:
    if checkpoint_namespace != "":
        raise ValueError("checkpoint_namespace must be empty for the root graph")
    if _CHECKPOINT_THREAD_ID_PATTERN.fullmatch(checkpoint_thread_id) is None:
        raise LegacyCheckpointIncompatibleError()
    selected_registry_factory = tool_registry_factory or getattr(
        tool_agent_node, "tool_registry_factory", None
    ) or get_default_tool_registry
    tool_registry_snapshot = _runtime_registry_snapshot(
        registry_factory=selected_registry_factory,
    )
    graph = build_question_answer_graph(
        business_intent_catalog=business_intent_catalog,
        skill_agent_node=skill_agent_node,
        tool_agent_node=tool_agent_node,
        store=store,
        trace_buffer=trace_buffer,
        trace_store=trace_store,
        checkpointer=checkpointer,
        llm_client=llm_client,
        tool_registry_factory=tool_registry_factory,
        tool_registry_snapshot=tool_registry_snapshot,
        tool_search_service=tool_search_service,
        allow_test_tool_agent_override=allow_test_tool_agent_override,
    )
    config = {
        "configurable": {
            "thread_id": checkpoint_thread_id,
            "checkpoint_ns": checkpoint_namespace,
            "checkpoint_id": checkpoint_id,
        }
    }
    snapshot = graph.get_state(config)
    interrupt_payload = _find_interrupt_payload(snapshot, interrupt_id=interrupt_id)
    if (
        interrupt_payload is None
        or interrupt_payload.get("approval_id") != approval_decision.get("approval_id")
        or interrupt_payload.get("action_digest") != action_digest
    ):
        raise ApprovalPayloadMismatchError("审批目标 Interrupt 与已保存快照不匹配。")
    checkpoint_digest = snapshot.values.get("tool_catalog_digest")
    if snapshot.values and checkpoint_digest != tool_registry_snapshot.catalog_digest:
        _record_resume_catalog_guard_trace(
            trace_buffer=trace_buffer,
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
    return graph.invoke(Command(resume=approval_decision), config=config)


def _find_interrupt_payload(snapshot: Any, *, interrupt_id: str) -> dict[str, Any] | None:
    for task in getattr(snapshot, "tasks", ()):
        for item in getattr(task, "interrupts", ()):
            if getattr(item, "id", None) == interrupt_id and isinstance(
                getattr(item, "value", None), dict
            ):
                return item.value
    return None


def _record_resume_catalog_guard_trace(
    *,
    trace_buffer: TraceBuffer | None,
    trace_store: InMemoryTraceStore | None,
    state: AgentState,
    error: BaseException,
) -> None:
    observer = NodeObserver(trace_buffer=trace_buffer, trace_store=trace_store)
    observation = observer.observe_start("catalog_guard", state)
    observer.record_error(
        observation=observation,
        state=state,
        error=error,
        error_code=getattr(error, "code", "agent_node_error"),
    )
