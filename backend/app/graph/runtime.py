import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer, Command, RetryPolicy, Send

from app.core.config import settings
from app.graph.catalog_errors import expired_catalog_update
from app.graph.nodes.approval_coordinator import approval_coordinator_node
from app.graph.nodes.approval_gate import (
    ApprovalPayloadMismatchError,
    ApprovalResumeError,
    build_approval_gate_node,
)
from app.graph.nodes.attachment_analyzer import (
    AttachmentContextResolver,
    build_attachment_analyzer_node,
)
from app.graph.nodes.intent_router import build_intent_router_node
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.nodes.skill_agent import build_skill_agent_node
from app.graph.nodes.skill_step_check import build_skill_step_check_node
from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.nodes.user_input_gate import user_input_gate_node
from app.graph.nodes.verifier import verifier_node
from app.graph.planning.contracts import PlanModel, SubTaskModel, TaskResultModel
from app.graph.planning.policy import (
    MAX_PLANNER_FEEDBACKS,
    MAX_WEB_SEARCH_TASKS,
    PLANNER_POLICY_VERSION,
    observation_signature,
)
from app.graph.planning.request_flow import pending_replan_valid, uses_request_flow
from app.graph.planning.scheduler import executable_ready_tasks
from app.graph.planning.skill_workflow import dispatch_workflow, workflow_failed
from app.graph.public_events import emit_public_progress
from app.graph.recovery.batching import (
    capture_dispatch,
    deferred_sends,
    dispatch_snapshot,
    pending_batch_update,
    settle_blocked_deferred,
)
from app.graph.recovery.executor import accept_confirmation, build_recovery_node
from app.graph.state import (
    AgentState,
    build_checkpoint_thread_id,
    create_initial_state,
)
from app.harness.error_boundary import (
    build_node_error_handler,
    build_safe_error_record,
    classify_recoverable_error,
    is_retryable_recoverable_error,
    is_terminal_recoverable_code,
    should_route_recoverable_error_to_synthesizer,
)
from app.harness.node_runner import NodeRunner
from app.harness.observer import NodeObserver
from app.harness.trace_buffer import TraceBuffer
from app.harness.tracing import InMemoryTraceStore, TraceEvent
from app.intent.catalog import BusinessIntentCatalog
from app.llm.factory import LlmClient
from app.operations.langfuse_observability import langfuse_agent_run, langfuse_observation
from app.operations.langfuse_scheduler import (
    record_recovery_event,
    record_scheduler_dispatch,
    record_scheduler_input,
)
from app.schemas.messages import Message
from app.schemas.recovery import RECOVERY_KIND, RECOVERY_VERSION
from app.skills.factory import build_default_skill_registry
from app.skills.registry import SkillRegistry
from app.tools.call_budget import SearchCallBudget, consumes_search_budget
from app.tools.candidate_resolver import ToolCandidateResolver
from app.tools.default_registry import (
    get_default_tool_catalog_runtime,
    get_default_tool_registry,
)
from app.tools.registry import ToolCatalogChangedError, ToolRegistry
from app.tools.search import ToolSearchService

_DEFAULT_TOOL_REGISTRY_FACTORY = get_default_tool_registry
_active_langfuse_finalizer: Any | None = None


def configure_langfuse_finalizer(finalizer: Any | None) -> None:
    """Inject the application-scoped, best-effort run finalizer."""
    global _active_langfuse_finalizer
    _active_langfuse_finalizer = finalizer


def _finalize_langfuse_run(handle: Any, state: Any) -> None:
    finalizer = _active_langfuse_finalizer
    if finalizer is None:
        return
    try:
        finalizer.finalize(handle, state)
    except Exception:  # noqa: BLE001 - telemetry must not alter business execution
        # Telemetry is isolated from the business graph even for injected finalizers.
        return


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
            "旧 Checkpoint 不可安全恢复：归属信息或规划策略版本不兼容，请重新执行请求。"
        )


def _validate_planner_checkpoint(state: dict) -> None:
    if state.get("planner_policy_version") != PLANNER_POLICY_VERSION:
        raise LegacyCheckpointIncompatibleError()


_CHECKPOINT_THREAD_ID_PATTERN = re.compile(r"cpr1_[0-9a-f]{64}")


@dataclass(frozen=True)
class AgentGraphStreamEvent:
    event: Literal["messages", "custom"]
    data: Any


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
    from app.security.public_output import public_text
    draft = state["draft_answer"]
    approval_status = state["approval_status"]
    state["final_response"] = {
        "content": public_text(draft["content"]) if draft else "没有生成答案。",
        "trace_id": state["trace_id"],
        "cited_evidence_ids": draft.get("cited_evidence_ids", []) if draft else [],
        "approval_status": approval_status.status if approval_status else "not_required",
    }
    if draft and draft.get("response_type"):
        state["final_response"]["response_type"] = draft["response_type"]
    from app.graph.planning.search_sources import collect_search_sources

    sources = collect_search_sources(state, draft.get("sources", []) if draft else [])
    if sources:
        state["final_response"]["sources"] = sources
    if draft and draft.get("business_error_code"):
        state["final_response"]["business_error_code"] = draft["business_error_code"]
    _append_assistant_message(state)
    return state


def _append_assistant_message(state: AgentState) -> None:
    final_response = state["final_response"] or {}
    content = final_response.get("content")
    if not content:
        return
    message_id = state["assistant_message_id"]
    if any(message.id == message_id for message in state["messages"]):
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
        # 节点可能只返回局部更新，也可能原地修改迭代号；首尾事件必须属于同一活动。
        progress_identity = {
            "active_task": dict(state.get("active_task") or {}),
            "planner_iteration": state.get("planner_iteration", 0),
        }
        emit_public_progress(node_name, state, status="started")
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
            emit_public_progress(
                node_name,
                {**state, **result, **progress_identity},
                status="completed",
            )
            return result
        except Exception as exc:
            emit_public_progress(
                node_name,
                {**state, **progress_identity},
                status="failed",
            )
            classified = classify_recoverable_error(exc)
            if classified is not None and should_route_recoverable_error_to_synthesizer(exc):
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


_OPERATIONAL_RETRY_POLICY = RetryPolicy(
    initial_interval=0.2,
    backoff_factor=2.0,
    max_interval=2.0,
    max_attempts=3,
    retry_on=is_retryable_recoverable_error,
)


def _retry_unpublished_synthesis(error: BaseException) -> bool:
    current = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "public_answer_started", False):
            return False
        current = current.__cause__ or current.__context__
    return is_retryable_recoverable_error(error)


def _node_retry_policy(node_name: str) -> tuple[RetryPolicy, ...] | None:
    if node_name in {"tool_agent", "permission_recovery"}:
        # 工具调用可能已经在上游产生副作用，不能因响应超时而重放整个节点。
        return ()
    if node_name == "synthesizer":
        return (_OPERATIONAL_RETRY_POLICY._replace(retry_on=_retry_unpublished_synthesis),)
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
    if not intent.get("intent_type"):
        return "synthesizer"
    return {"direct_answer": "verifier", "synthesize": "synthesizer", "plan": "planner_agent"}[
        intent.get("route", "plan")
    ]


def _route_after_synthesis(state: AgentState) -> str:
    if uses_request_flow(state):
        if pending_replan_valid(state):
            return "planner_agent"
        if state.get("draft_answer") is None and state.get("replan_stop_reason"):
            return "synthesizer"
    return "verifier"


def task_scheduler_node(state: AgentState) -> dict[str, Any]:
    from app.graph.nodes.permission_fallback import PERMISSION_CODES, pending_permission_request

    update = {}
    if state.get("recovery_policy_version") == RECOVERY_VERSION:
        update.update(pending_batch_update(state))
    elif (
        not state.get("planner_stop_reason")
        and state.get("pending_user_input") is None
        and state.get("user_input_response") is None
    ):
        pending_permission = pending_permission_request(state)
        if pending_permission is not None:
            update["pending_user_input"] = pending_permission
        elif not uses_request_flow(state) and any(
            item.get("error_code") in PERMISSION_CODES for item in state.get("task_results", [])
        ):
            update["planner_stop_reason"] = "provider_permission_denied"
    count = len(state.get("tool_discovery_events", {}))
    if count != state.get("tool_search_count", 0):
        update["tool_search_count"] = count
    if (
        not uses_request_flow(state)
        and
        "planner_stop_reason" not in update
        and dispatch_workflow(state) is None
        and state.get("plan")
        and state.get("planner_feedback_count", 0) >= MAX_PLANNER_FEEDBACKS
    ):
        signature = observation_signature(
            PlanModel.model_validate(state["plan"]), state.get("task_results", [])
        )
        if signature != state.get("planner_reviewed_signature"):
            update["planner_stop_reason"] = "planner_feedback_budget_exhausted"
    return update


def build_task_scheduler_node(registry: ToolRegistry):
    def schedule(state):
        blocked = settle_blocked_deferred(state, registry) if state.get("recovery_policy_version") == RECOVERY_VERSION else {}
        staged_state = {**state, **blocked}
        if blocked.get("recovery_items"):
            staged_state["recovery_items"] = {
                **state.get("recovery_items", {}), **blocked["recovery_items"],
            }
        if blocked.get("task_results"):
            from app.graph.state import merge_task_results

            staged_state["task_results"] = merge_task_results(state.get("task_results"), blocked["task_results"])
        return {**blocked, **task_scheduler_node(staged_state)}
    return schedule


def _dispatch_after_scheduler(state: AgentState) -> str | list[Send]:
    with langfuse_observation(as_type="span", name="dispatch_task_scheduler") as observation:
        record_scheduler_input(observation, state)
        return _dispatch_after_scheduler_impl(state, observation)


def _dispatch_after_scheduler_impl(state: AgentState, observation: Any) -> str | list[Send]:
    if state.get("pending_user_input") is not None:
        return record_scheduler_dispatch(observation, "user_input_gate", "user_input_required")
    response = state.get("user_input_response")
    if response is not None and response.source.kind == RECOVERY_KIND:
        return record_scheduler_dispatch(observation, "permission_recovery", "permission_recovery_confirmed")
    if state.get("recovery_policy_version") == RECOVERY_VERSION:
        deferred = deferred_sends(state)
        if deferred:
            return record_scheduler_dispatch(observation, deferred, "settled_peer_allows_deferred_dispatch")
    if response is not None and response.source.kind == "provider_permission_fallback":
        # 先消费原调用的响应，避免并行结果触发重新规划而丢失恢复目标。
        context = state.get("tool_selection_contexts", {}).get(response.source.call_id, {})
        task = context.get("task") or next(
            (
                item for item in (state.get("plan") or {}).get("tasks", [])
                if item.get("id") == response.source.task_id
            ),
            state.get("active_task"),
        )
        task = task or {"id": "permission_resume_invalid", "task_goal": "核实权限恢复请求"}
        return record_scheduler_dispatch(
            observation,
            [Send("tool_agent", {**state, "active_task": task, "active_skill_step": None})],
            "permission_fallback_resume",
        )
    if _has_pending_memory_approval(state) or _has_pending_tool_approval(state):
        return record_scheduler_dispatch(observation, "approval_gate", "approval_required")
    if workflow_failed(state) and not uses_request_flow(state):
        return record_scheduler_dispatch(observation, "synthesizer", "skill_workflow_failed")
    workflow_dispatch = dispatch_workflow(state)
    if workflow_dispatch is not None:
        return record_scheduler_dispatch(observation, workflow_dispatch, "skill_workflow_dispatch")
    # 旧字段同时承载 Skill/恢复的局部失败；新链路以任务结果推进，
    # 修订控制只使用 replan_stop_reason，不能让局部失败截断独立任务。
    if state.get("planner_stop_reason") and not uses_request_flow(state):
        return record_scheduler_dispatch(
            observation, "synthesizer", "planner_stopped",
            planner_stop_reason=state["planner_stop_reason"],
        )
    if uses_request_flow(state) and state.get("replan_stop_reason"):
        return record_scheduler_dispatch(observation, "synthesizer", state["replan_stop_reason"])
    plan = PlanModel.model_validate(state.get("plan"))
    results = [TaskResultModel.model_validate(item) for item in state.get("task_results", [])]
    signature = observation_signature(plan, state.get("task_results", []))
    reviewed = state.get("planner_reviewed_signature") == signature
    if reviewed and (state.get("planner_decision") or {}).get("action") == "finish":
        return record_scheduler_dispatch(observation, "synthesizer", "planner_finished")
    active_ids = {task.id for task in plan.tasks}
    has_observations = any(result.task_id in active_ids for result in results)
    pending_sources = any(task_id in active_ids and value.get("status") in {"ready", "waiting"}
                          for task_id, value in state.get("source_attempt_queues", {}).items())
    if has_observations and not reviewed and not uses_request_flow(state) and not pending_sources:
        if state.get("planner_feedback_count", 0) >= MAX_PLANNER_FEEDBACKS:
            return record_scheduler_dispatch(
                observation, "synthesizer", "planner_feedback_budget_exhausted",
            )
        return record_scheduler_dispatch(observation, "planner_agent", "new_results_require_review")
    ready = executable_ready_tasks(plan, results=results)
    if not ready:
        return record_scheduler_dispatch(observation, "synthesizer", "no_ready_tasks")
    used_searches = sum(
        1
        for record in state.get("tool_calls", [])
        if consumes_search_budget(record)
    )
    remaining_web_slots = max(0, MAX_WEB_SEARCH_TASKS - used_searches)
    sends: list[Send] = []
    # 固定工作流串行启动，避免与其他任务并发审批或修改调度状态。
    skill_task = next((task for task in ready if task.skill_name is not None), None)
    if skill_task is not None:
        skill_payload = {**state, "active_task": skill_task.model_dump(), "active_skill_step": None}
        if state.get("recovery_policy_version") == RECOVERY_VERSION:
            skill_payload["active_dispatch_batch"] = dispatch_snapshot(state, [skill_task])
        return record_scheduler_dispatch(observation, [
            Send(
                "skill_agent",
                skill_payload,
            )
        ], "skill_task_ready")
    for task in ready:
        worker = _worker_name(task)
        payload = {
            **state,
            "active_task": task.model_dump(mode="python"),
            "active_skill_step": None,
        }
        if worker == "tool_agent":
            payload["web_search_allowed"] = remaining_web_slots > 0
        if state.get("recovery_policy_version") == RECOVERY_VERSION:
            payload["active_dispatch_batch"] = dispatch_snapshot(state, ready)
        sends.append(Send(worker, payload))
    return record_scheduler_dispatch(
        observation, sends, "ready_tasks", remaining_web_search_slots=remaining_web_slots,
    )


def _worker_name(task: SubTaskModel) -> str:
    if task.execution_type != "tool":
        raise ValueError("当前运行链路不执行独立分析任务。")
    if task.skill_name is not None:
        return "skill_agent"
    return "tool_agent"


def _route_after_approval_gate(state: AgentState) -> str:
    if state["approval_status"] is not None and state["approval_status"].status == "rejected":
        return "synthesizer"
    return "task_scheduler"


def _has_pending_tool_approval(state: AgentState) -> bool:
    return state["pending_tool_approval"] is not None


def _has_pending_memory_approval(state: AgentState) -> bool:
    return any(
        candidate.approval_status == "pending" and candidate.sensitivity in {"medium", "high"}
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
    selected_registry_factory = (
        tool_registry_factory
        or getattr(tool_agent_node, "tool_registry_factory", None)
        or get_default_tool_registry
    )
    tool_registry_snapshot = tool_registry_snapshot or _runtime_registry_snapshot(
        registry_factory=selected_registry_factory,
    )
    selected_tool_search_service = _resolve_tool_search_service(
        explicit_service=tool_search_service,
        registry_factory=selected_registry_factory,
        registry_snapshot=tool_registry_snapshot,
    )
    selected_skill_registry = (
        build_default_skill_registry() if skill_agent_node is None else SkillRegistry()
    )
    selected_skill_agent_node = skill_agent_node or build_skill_agent_node(
        registry=selected_skill_registry,
        llm_client=llm_client,
        tool_registry=tool_registry_snapshot,
    )
    search_budget = SearchCallBudget(limit=MAX_WEB_SEARCH_TASKS)
    if tool_agent_node is not None and not getattr(tool_agent_node, "is_trusted_tool_agent", False):
        if not allow_test_tool_agent_override:
            raise ValueError("任意 ToolAgent callable 仅允许通过显式测试钩子注入。")
        selected_tool_agent_node = tool_agent_node
    else:
        selected_tool_agent_node = build_tool_agent_node(
            registry_factory=lambda: tool_registry_snapshot,
            search_budget=search_budget,
            llm_client=llm_client,
            candidate_resolver=(
                ToolCandidateResolver(search_service=selected_tool_search_service)
                if selected_tool_search_service is not None
                else None
            ),
            availability_registry_factory=(
                (lambda: get_default_tool_catalog_runtime().snapshot_pair_for_run()[0])
                if selected_registry_factory is _DEFAULT_TOOL_REGISTRY_FACTORY
                else selected_registry_factory
            ),
        )
    graph = StateGraph(AgentState).set_node_defaults(
        retry_policy=_OPERATIONAL_RETRY_POLICY,
    )
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
    # 仅用于恢复入口将不可恢复的 run 快照投影为模型可见错误，不切换节点目录。
    _add_harnessed_node(graph, "catalog_error", lambda state: {}, runner)
    graph.add_edge("catalog_error", "synthesizer")
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
    _add_harnessed_node(graph, "task_scheduler", build_task_scheduler_node(tool_registry_snapshot), runner)
    _add_harnessed_node(
        graph, "skill_step_check", build_skill_step_check_node(llm_client=llm_client), runner
    )
    _add_harnessed_node(
        graph,
        "skill_agent",
        capture_dispatch(selected_skill_agent_node),
        runner,
    )
    _add_harnessed_node(
        graph,
        "tool_agent",
        capture_dispatch(selected_tool_agent_node),
        runner,
    )
    _add_harnessed_node(
        graph,
        "approval_gate",
        build_approval_gate_node(tool_catalog_digest=tool_registry_snapshot.catalog_digest),
        runner,
    )
    def input_gate(state):
        return user_input_gate_node(state, accept_recovery=lambda *args: accept_confirmation(*args, registry=tool_registry_snapshot))

    _add_harnessed_node(graph, "user_input_gate", input_gate, runner)
    _add_harnessed_node(graph, "permission_recovery", build_recovery_node(tool_registry_snapshot,
        availability_registry_factory=(
            (lambda: get_default_tool_catalog_runtime().snapshot_pair_for_run()[0])
            if selected_registry_factory is _DEFAULT_TOOL_REGISTRY_FACTORY else selected_registry_factory
        )), runner)
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

    def finish_response(state):
        search_budget.release(
            json.dumps([state["tenant_id"], state["session_id"], state["request_id"]])
        )
        result = final_response_node(state)
        get_default_tool_catalog_runtime().release_run_snapshot(build_checkpoint_thread_id(
            tenant_id=state["tenant_id"], session_key=state["session_key"], request_id=state["request_id"],
        ))
        return result

    _add_harnessed_node(graph, "final_response", finish_response, runner)

    graph.set_entry_point("catalog_guard")
    graph.add_edge("catalog_guard", "attachment_analyzer")
    graph.add_edge("attachment_analyzer", "intent_router")
    graph.add_conditional_edges(
        "intent_router",
        _route_after_intent,
        {
            "planner_agent": "planner_agent",
            "synthesizer": "synthesizer",
            "verifier": "verifier",
        },
    )
    graph.add_edge("planner_agent", "task_scheduler")
    graph.add_conditional_edges(
        "task_scheduler",
        _dispatch_after_scheduler,
        {
            "planner_agent": "planner_agent",
            "approval_gate": "approval_gate",
            "user_input_gate": "user_input_gate",
            "permission_recovery": "permission_recovery",
            "synthesizer": "synthesizer",
        },
    )
    graph.add_edge("skill_agent", "task_scheduler")
    graph.add_edge("skill_step_check", "task_scheduler")
    graph.add_edge("tool_agent", "task_scheduler")
    graph.add_edge("user_input_gate", "task_scheduler")
    graph.add_edge("permission_recovery", "task_scheduler")
    graph.add_conditional_edges(
        "approval_gate",
        _route_after_approval_gate,
        {
            "task_scheduler": "task_scheduler",
            "synthesizer": "synthesizer",
        },
    )
    graph.add_conditional_edges("synthesizer", _route_after_synthesis, {
        "verifier": "verifier", "planner_agent": "planner_agent", "synthesizer": "synthesizer",
    })
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
    with langfuse_agent_run(
        business_trace_id=state["trace_id"],
        session_key=state.get("session_key") or session_id,
        tenant_id=state["tenant_id"],
        execution_segment="initial",
        user_id=user_id,
        input=user_message,
    ) as callbacks:
        try:
            result = graph.invoke(
                state,
                config=_config_with_callbacks(config, callbacks),
            )
        except Exception:
            _finalize_langfuse_run(callbacks, _langfuse_failure_snapshot(state))
            raise
        final_output = _langfuse_final_output(result)
        if final_output is not None and _active_langfuse_finalizer is None:
            callbacks.update(output=final_output)
        if _has_terminal_run_state(result):
            _finalize_langfuse_run(callbacks, result)
        return result


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
) -> Iterator[AgentState | AgentGraphStreamEvent]:
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
    with langfuse_agent_run(
        business_trace_id=state["trace_id"],
        session_key=state.get("session_key") or session_id,
        tenant_id=state["tenant_id"],
        execution_segment="initial",
        user_id=user_id,
        input=user_message,
    ) as callbacks:
        latest_state = state
        state_emitted = False
        finalized = False
        try:
            for mode, update in graph.stream(
                state,
                config=_config_with_callbacks(config, callbacks),
                stream_mode=["updates", "messages", "custom"],
            ):
                if mode in {"messages", "custom"}:
                    yield AgentGraphStreamEvent(event=mode, data=update)
                    continue
                next_state = _state_from_stream_update(update, fallback=latest_state)
                if next_state is not None:
                    latest_state = next_state
                    state_emitted = True
                    yield latest_state
        except Exception:
            _finalize_langfuse_run(callbacks, _langfuse_failure_snapshot(latest_state))
            raise
        if (
            not finalized
            and state_emitted
            and not latest_state.get("__interrupt__")
            and _has_terminal_run_state(latest_state)
        ):
            final_output = _langfuse_final_output(latest_state)
            if final_output is not None and _active_langfuse_finalizer is None:
                callbacks.update(output=final_output)
            _finalize_langfuse_run(callbacks, latest_state)
            finalized = True


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
    selected_registry_factory = (
        tool_registry_factory
        or getattr(tool_agent_node, "tool_registry_factory", None)
        or get_default_tool_registry
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
        attachment_ids=attachment_ids,
    )
    if selected_registry_factory is _DEFAULT_TOOL_REGISTRY_FACTORY and tool_search_service is None:
        tool_registry_snapshot, tool_search_service = get_default_tool_catalog_runtime().snapshot_pair_for_run(
            run_key=build_checkpoint_thread_id(tenant_id=state["tenant_id"],
                session_key=state["session_key"], request_id=state["request_id"]),
        )
    else:
        tool_registry_snapshot = _runtime_registry_snapshot(registry_factory=selected_registry_factory)
    state["tool_catalog_digest"] = tool_registry_snapshot.catalog_digest
    try:
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
    except Exception:
        if selected_registry_factory is _DEFAULT_TOOL_REGISTRY_FACTORY:
            get_default_tool_catalog_runtime().release_run_snapshot(build_checkpoint_thread_id(
                tenant_id=state["tenant_id"], session_key=state["session_key"], request_id=state["request_id"],
            ))
        raise
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


def _config_with_callbacks(
    config: dict | None,
    callbacks: tuple[Any, ...],
) -> dict | None:
    if not callbacks:
        return config
    merged = dict(config or {})
    merged["callbacks"] = [*merged.get("callbacks", ()), *callbacks]
    return merged


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
            return {**fallback, **node_update}
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
    selected_registry_factory = (
        tool_registry_factory
        or getattr(tool_agent_node, "tool_registry_factory", None)
        or get_default_tool_registry
    )
    if selected_registry_factory is _DEFAULT_TOOL_REGISTRY_FACTORY and tool_search_service is None:
        catalog = get_default_tool_catalog_runtime()
        tool_registry_snapshot, tool_search_service = (
            catalog.get_run_snapshot(checkpoint_thread_id) or catalog.snapshot_pair_for_run()
        )
    else:
        tool_registry_snapshot = _runtime_registry_snapshot(registry_factory=selected_registry_factory)
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
        "recursion_limit": settings.graph_recursion_limit,
        "configurable": {
            "thread_id": checkpoint_thread_id,
            "checkpoint_ns": checkpoint_namespace,
            "checkpoint_id": checkpoint_id,
        },
    }
    snapshot = graph.get_state(config)
    if snapshot.values:
        _validate_planner_checkpoint(snapshot.values)
    interrupt_payload = _find_interrupt_payload(snapshot, interrupt_id=interrupt_id)
    if (
        interrupt_payload is None
        or interrupt_payload.get("approval_id") != approval_decision.get("approval_id")
        or interrupt_payload.get("action_digest") != action_digest
    ):
        raise ApprovalPayloadMismatchError("审批目标 Interrupt 与已保存快照不匹配。")
    checkpoint_digest = snapshot.values.get("tool_catalog_digest")
    if snapshot.values and checkpoint_digest != tool_registry_snapshot.catalog_digest:
        if approval_decision.get("action_digest") != action_digest:
            raise ApprovalPayloadMismatchError("审批决定与原动作摘要不匹配。")
        if approval_decision.get("decision") not in {"approved", "rejected"}:
            raise ApprovalResumeError("审批决定必须为 approved 或 rejected。")
        _record_resume_catalog_guard_trace(
            trace_buffer=trace_buffer,
            trace_store=trace_store,
            state=snapshot.values,
            error=ToolCatalogChangedError(),
        )
        resumed_config = graph.update_state(config, expired_catalog_update(snapshot.values), as_node="catalog_error")
        return graph.invoke(None, config=resumed_config)
    if selected_registry_factory is _DEFAULT_TOOL_REGISTRY_FACTORY:
        get_default_tool_catalog_runtime().retain_run_snapshot(
            checkpoint_thread_id, (tool_registry_snapshot, tool_search_service),
        )
    with langfuse_agent_run(
        business_trace_id=snapshot.values["trace_id"],
        session_key=snapshot.values["session_key"],
        tenant_id=snapshot.values["tenant_id"],
        execution_segment="resume",
        user_id=snapshot.values.get("user_id"),
        input=_langfuse_root_input(snapshot.values),
    ) as callbacks:
        try:
            result = graph.invoke(
                Command(resume=approval_decision),
                config=_config_with_callbacks(config, callbacks),
            )
        except Exception:
            _finalize_langfuse_run(
                callbacks,
                _langfuse_failure_snapshot(snapshot.values),
            )
            raise
        final_output = _langfuse_final_output(result)
        if final_output is not None and _active_langfuse_finalizer is None:
            callbacks.update(output=final_output)
        if _has_terminal_run_state(result):
            _finalize_langfuse_run(callbacks, result)
        return result


def _langfuse_final_output(state: Any) -> Any:
    if isinstance(state, dict):
        final_response = state.get("final_response")
        if final_response is not None:
            return final_response
        errors = state.get("errors")
        if errors:
            return {"errors": errors}
    return None


def _has_terminal_run_state(state: Any) -> bool:
    return isinstance(state, dict) and (
        state.get("final_response") is not None or bool(state.get("errors"))
    )


def _langfuse_failure_snapshot(state: Any) -> dict[str, Any]:
    """Build a bounded failure record without copying exception text or private locals."""
    source = state if isinstance(state, dict) else {}
    allowed = (
        "trace_id",
        "request_id",
        "user_message",
        "messages",
        "tool_calls",
        "approval_requests",
        "approved_tool_calls",
        "approval_status",
        "selection_observation_id",
        "selection_confidence",
    )
    snapshot = {key: source[key] for key in allowed if key in source}
    snapshot["errors"] = [
        {
            "node": "graph",
            "code": "agent_graph_execution_failed",
            "http_status": 500,
            "handled": False,
            "retryable": False,
        }
    ]
    return snapshot


def _langfuse_root_input(state: AgentState) -> Any:
    messages = state.get("messages", [])
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.content
        if isinstance(message, dict) and message.get("type") == "human":
            return message.get("content")
    return None


def resume_user_input_graph(
    *,
    business_intent_catalog: BusinessIntentCatalog,
    checkpoint_thread_id: str,
    interrupt_id: str,
    interaction_id: str,
    input_type: Literal["selection"],
    value: str,
    presentation_version: str | None = None,
    batch_digest: str | None = None,
    expected_identity: dict[str, str] | None = None,
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
    stream_events: bool = False,
) -> AgentState | Iterator[AgentState | AgentGraphStreamEvent]:
    if _CHECKPOINT_THREAD_ID_PATTERN.fullmatch(checkpoint_thread_id) is None:
        raise LegacyCheckpointIncompatibleError()
    selected_registry_factory = (
        tool_registry_factory
        or getattr(tool_agent_node, "tool_registry_factory", None)
        or get_default_tool_registry
    )
    if selected_registry_factory is _DEFAULT_TOOL_REGISTRY_FACTORY and tool_search_service is None:
        catalog = get_default_tool_catalog_runtime()
        tool_registry_snapshot, tool_search_service = (
            catalog.get_run_snapshot(checkpoint_thread_id) or catalog.snapshot_pair_for_run()
        )
    else:
        tool_registry_snapshot = _runtime_registry_snapshot(registry_factory=selected_registry_factory)
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
        "recursion_limit": settings.graph_recursion_limit,
        "configurable": {
            "thread_id": checkpoint_thread_id,
            "checkpoint_ns": "",
        },
    }
    snapshot = graph.get_state(config)
    if expected_identity is not None and (
        not snapshot.values
        or any(snapshot.values.get(key) != value for key, value in expected_identity.items())
    ):
        record_recovery_event("recovery_invalidated", request_id=snapshot.values.get("request_id"),
            route_reason="user_input_resume_mismatch")
        raise AgentGraphExecutionError(
            node_name="user_input_resume", message="恢复状态归属与原会话不匹配。",
            code="user_input_resume_mismatch", http_status=409,
        )
    if snapshot.values:
        _validate_planner_checkpoint(snapshot.values)
    interrupt_payload = _find_interrupt_payload(snapshot, interrupt_id=interrupt_id)
    if (
        interrupt_payload is None
        or interrupt_payload.get("interrupt_type") != "user_input"
        or interrupt_payload.get("interaction_id") != interaction_id
        or interrupt_payload.get("input_type") != input_type
    ):
        raise AgentGraphExecutionError(
            node_name="user_input_gate",
            message="用户输入恢复目标与当前 Interrupt 不匹配。",
            code="user_input_resume_mismatch",
            http_status=409,
        )
    presentation = interrupt_payload.get("recovery_batch")
    pending = snapshot.values.get("pending_user_input")
    pending_kind = pending.source.kind if pending is not None else None
    if (
        pending_kind == RECOVERY_KIND and (presentation is None or snapshot.values.get("recovery_policy_version") != RECOVERY_VERSION)
        or pending_kind == "provider_permission_fallback" and snapshot.values.get("recovery_policy_version") == RECOVERY_VERSION
    ):
        record_recovery_event("recovery_invalidated", request_id=snapshot.values.get("request_id"),
            route_reason="permission_recovery_version_mismatch")
        raise AgentGraphExecutionError(
            node_name="user_input_resume", message="权限恢复版本或完整批次元数据缺失，请重新发起请求。",
            code="permission_recovery_version_mismatch", http_status=409,
        )
    if presentation is not None and (
        not isinstance(presentation, dict)
        or presentation_version != presentation.get("version")
        or batch_digest != presentation.get("digest")
    ):
        record_recovery_event("recovery_invalidated", request_id=snapshot.values.get("request_id"),
            batch_id=presentation.get("batch_id") if isinstance(presentation, dict) else None,
            route_reason="permission_recovery_presentation_mismatch")
        raise AgentGraphExecutionError(
            node_name="user_input_resume", message="恢复任务清单或显示协议不匹配，请刷新后重新确认。",
            code="permission_recovery_presentation_mismatch", http_status=409,
        )
    if presentation is not None:
        from app.schemas.recovery import RecoveryBatchView

        try:
            presented = RecoveryBatchView.model_validate(presentation)
            if pending is None or pending.recovery_batch != presented:
                raise ValueError("presentation_changed")
        except ValueError as exc:
            record_recovery_event("recovery_invalidated", request_id=snapshot.values.get("request_id"),
                route_reason="permission_recovery_presentation_mismatch")
            raise AgentGraphExecutionError(
                node_name="user_input_resume", message="显示的恢复任务清单与服务端授权快照不一致。",
                code="permission_recovery_presentation_mismatch", http_status=409,
            ) from exc
    checkpoint_digest = snapshot.values.get("tool_catalog_digest")
    if snapshot.values and checkpoint_digest != tool_registry_snapshot.catalog_digest:
        if value not in {option.get("value") for option in interrupt_payload.get("options", []) if isinstance(option, dict)}:
            raise AgentGraphExecutionError(node_name="user_input_gate", message="用户选择不在服务端发布的候选项中。",
                code="user_input_selection_invalid", http_status=409)
        _record_resume_catalog_guard_trace(
            trace_buffer=trace_buffer,
            trace_store=trace_store,
            state=snapshot.values,
            error=ToolCatalogChangedError(),
        )
        resumed_config = graph.update_state(config, expired_catalog_update(snapshot.values), as_node="catalog_error")
        return (_stream_resumed_graph(graph, None, resumed_config) if stream_events
                else graph.invoke(None, config=resumed_config))
    if presentation is not None:
        from app.graph.recovery.validation import validate_batch

        validation_state = {**snapshot.values}
        if permission_scopes is not None:
            validation_state["permission_scopes"] = list(permission_scopes)
        try:
            validate_batch(validation_state, snapshot.values["pending_user_input"], registry=tool_registry_snapshot)
        except ValueError as exc:
            raise AgentGraphExecutionError(
                node_name="user_input_resume", message="权限恢复授权或快照不匹配，请重新发起请求。",
                code=getattr(exc, "code", "permission_recovery_snapshot_mismatch"), http_status=409,
            ) from exc
    if selected_registry_factory is _DEFAULT_TOOL_REGISTRY_FACTORY:
        get_default_tool_catalog_runtime().retain_run_snapshot(
            checkpoint_thread_id, (tool_registry_snapshot, tool_search_service),
        )
    command = Command(
        # 来自恢复请求已认证 Principal；空列表表示权限已撤销，不能回退旧快照。
        update={"permission_scopes": list(permission_scopes)} if permission_scopes is not None else None,
        resume={
            "interaction_id": interaction_id,
            "input_type": input_type,
            "value": value,
            **({"presentation_version": presentation_version, "batch_digest": batch_digest}
               if presentation_version is not None or batch_digest is not None else {}),
        }
    )
    return (_stream_resumed_graph(graph, command, config) if stream_events
            else graph.invoke(command, config=config))


def _stream_resumed_graph(graph, command, config) -> Iterator[AgentState | AgentGraphStreamEvent]:
    """逐项转发公开事件，最后读取完整检查点，保留后续中断。"""
    for event in graph.stream(command, config=config, stream_mode="custom"):
        yield AgentGraphStreamEvent(event="custom", data=event)
    # update_state 返回的配置固定到旧 checkpoint；执行结束后必须读同线程最新状态。
    latest_config = {**config, "configurable": {
        key: value for key, value in config.get("configurable", {}).items()
        if key != "checkpoint_id"
    }}
    snapshot = graph.get_state(latest_config)
    state = dict(snapshot.values)
    interrupts = [item for task in snapshot.tasks for item in task.interrupts]
    if interrupts:
        state["__interrupt__"] = interrupts
    yield state


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
