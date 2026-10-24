"""真实 Gateway、独立回执和 Graph 的受控批次恢复，不访问外部服务。"""

import pytest
from app.conversations.execution import execution_control
from app.graph.nodes import tool_agent
from app.graph.nodes.user_input_gate import user_input_gate_node
from app.graph.recovery.batching import capture_dispatch
from app.graph.recovery.executor import accept_confirmation, build_recovery_node
from app.graph.runtime import _dispatch_after_scheduler, build_task_scheduler_node
from app.graph.state import AgentState, create_initial_state
from app.schemas.recovery import RECOVERY_VERSION
from app.storage.models import AgentMessage, AgentRun
from app.tests.unit.test_durable_turns import database  # noqa: F401
from app.tests.unit.test_permission_fallback import setup_worker
from app.tests.unit.test_recovery_durability import control as durable_control  # noqa: F401
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command
from sqlalchemy import select


@pytest.fixture
def control(request):
    return request.getfixturevalue("durable_control")


def batch_graph(control, monkeypatch, *, recovery_hook=None, policy=None):
    worker, _, data, search, client, registry = setup_worker()
    # 明确的受控业务契约替身，不让 LLM 或全局目录声明来源等价。
    original_context = tool_agent.selection_context

    def context(*args, **kwargs):
        value = original_context(*args, **kwargs)
        value["source_policy"] = (
            policy
            if policy is not None
            else {"public_alternative": "allowed", "basis": ["test-contract:public-materials-v1"]}
        )
        return value

    monkeypatch.setattr(tool_agent, "selection_context", context)
    with control.session_factory() as db:
        run = db.get(AgentRun, control.run_id)
        assistant = db.scalar(
            select(AgentMessage).where(
                AgentMessage.run_id == run.run_id, AgentMessage.role == "assistant"
            )
        )
        state = create_initial_state(
            session_id=run.session_id,
            session_key=run.session_key,
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            user_message="查询利润表和现金流",
            request_id="r",
            assistant_message_id=assistant.message_id,
            trace_id=run.trace_id,
            tool_catalog_digest=registry.catalog_digest,
            permission_scopes=["web:search"],
        )
    state["recovery_policy_version"] = RECOVERY_VERSION
    state["plan"] = {
        "tasks": [
            {"id": "income", "task_goal": "查询利润表", "completion_criteria": ["保留报告期间"]},
            {"id": "cashflow", "task_goal": "查询现金流", "completion_criteria": ["保留报告期间"]},
            {"id": "healthy", "task_goal": "分析已有材料", "execution_type": "analysis"},
        ]
    }
    builder = StateGraph(AgentState)
    builder.add_node("task_scheduler", build_task_scheduler_node(registry))
    builder.add_node("tool_agent", capture_dispatch(worker))
    builder.add_node(
        "analysis_agent",
        capture_dispatch(
            lambda state: {
                "task_results": [
                    {"task_id": "healthy", "status": "success", "content": "健康任务结果"}
                ]
            }
        ),
    )
    builder.add_node(
        "user_input_gate",
        lambda state: user_input_gate_node(
            state, accept_recovery=lambda *args: accept_confirmation(*args, registry=registry)
        ),
    )
    recovery = build_recovery_node(registry)

    def recover(state):
        update = recovery(state)
        if recovery_hook:
            recovery_hook(update)
        return update

    builder.add_node("permission_recovery", recover)
    builder.add_node("planner_agent", lambda state: {})
    builder.add_node("synthesizer", lambda state: {})
    builder.set_entry_point("task_scheduler")
    builder.add_conditional_edges("task_scheduler", _dispatch_after_scheduler)
    for node in ("tool_agent", "analysis_agent", "user_input_gate", "permission_recovery"):
        builder.add_edge(node, "task_scheduler")
    builder.add_edge("planner_agent", END)
    builder.add_edge("synthesizer", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    return graph, state, data, search, client, registry


@pytest.mark.parametrize("choice", ["web_search", "use_existing"])
def test_same_dispatch_refusals_form_one_complete_interrupt_without_model_reselection(
    control, monkeypatch, choice
):
    graph, state, data, search, client, _ = batch_graph(control, monkeypatch)
    config = {"configurable": {"thread_id": "batch"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        assert "__interrupt__" in paused, [row.get("error_code") for row in paused["task_results"]]
        assert len(paused["__interrupt__"]) == 1
        payload = paused["__interrupt__"][0].value
        assert len(payload["recovery_batch"]["items"]) == 2
        assert search.calls == 0 and data.calls == client.calls == 2
        resumed = graph.invoke(
            Command(
                resume={
                    "interaction_id": payload["interaction_id"],
                    "input_type": "selection",
                    "value": choice,
                    "presentation_version": payload["recovery_batch"]["version"],
                    "batch_digest": payload["recovery_batch"]["digest"],
                }
            ),
            config,
        )
    assert "__interrupt__" not in resumed
    assert data.calls == client.calls == 2
    assert search.calls == (2 if choice == "web_search" else 0)
    assert {item["task_id"] for item in resumed["task_results"]} == {
        "income",
        "cashflow",
        "healthy",
    }
    assert (
        next(item for item in resumed["task_results"] if item["task_id"] == "healthy")["content"]
        == "健康任务结果"
    )
    assert "planner_stop_reason" not in resumed


def confirm(payload, value="web_search"):
    return Command(
        resume={
            "interaction_id": payload["interaction_id"],
            "input_type": "selection",
            "value": value,
            "presentation_version": payload["recovery_batch"]["version"],
            "batch_digest": payload["recovery_batch"]["digest"],
        }
    )


def test_results_landed_before_checkpoint_failure_are_reused_without_remote_replay(
    control, monkeypatch
):
    failed = []

    def fail_once(_update):
        if not failed:
            failed.append(True)
            raise RuntimeError("controlled checkpoint window")

    graph, state, data, search, client, _ = batch_graph(
        control, monkeypatch, recovery_hook=fail_once
    )
    config = {"configurable": {"thread_id": "crash"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        with pytest.raises(RuntimeError, match="controlled checkpoint window"):
            graph.invoke(confirm(paused["__interrupt__"][0].value), config)
        assert search.calls == 2
        resumed = graph.invoke(None, config)
    assert search.calls == data.calls == client.calls == 2
    assert len(resumed["task_results"]) == 3


@pytest.mark.parametrize(
    "change", ["selection", "goal", "digest", "revoked", "source_policy", "account", "dispatch"]
)
def test_changed_authorization_is_rejected_before_search(control, monkeypatch, change):
    from copy import deepcopy

    from app.graph.nodes.user_input_gate import UserInputResumeError
    from app.graph.recovery.batching import batch_request
    from app.schemas.recovery import RecoveryBatch

    graph, state, _, search, _, registry = batch_graph(control, monkeypatch)
    with execution_control(control):
        paused = graph.invoke(state, {"configurable": {"thread_id": "tamper"}})
        altered = deepcopy(paused)
        batch = RecoveryBatch.model_validate(next(iter(paused["recovery_batches"].values())))
        first = batch.items[0]
        if change == "selection":
            altered["tool_selection_contexts"][first.original_call_id]["candidates"] = []
        elif change == "goal":
            altered["plan"]["tasks"][0]["task_goal"] = "更改目标"
        elif change == "digest":
            altered["recovery_batches"][batch.batch_id]["digest"] = "0" * 64
        elif change == "revoked":
            altered["permission_scopes"] = []
        elif change == "source_policy":
            altered["tool_selection_contexts"][first.original_call_id]["source_policy"][
                "public_alternative"
            ] = "forbidden"
        elif change == "account":
            altered["account_revision"] = "new-account"
        elif change == "dispatch":
            altered["dispatch_batches"][batch.dispatch_batch_id]["members"] = [first.task_id]
        with pytest.raises(UserInputResumeError):
            accept_confirmation(altered, batch_request(batch), "web_search", registry=registry)
    assert search.calls == 0


def test_confirmed_batch_cannot_reverse_choice(control, monkeypatch):
    from app.graph.nodes.user_input_gate import UserInputResumeError
    from app.graph.recovery.batching import batch_request
    from app.schemas.recovery import RecoveryBatch

    graph, state, _, search, _, registry = batch_graph(control, monkeypatch)
    with execution_control(control):
        paused = graph.invoke(state, {"configurable": {"thread_id": "reverse"}})
        batch = RecoveryBatch.model_validate(next(iter(paused["recovery_batches"].values())))
        request = batch_request(batch)
        accept_confirmation(paused, request, "use_existing", registry=registry)
        accept_confirmation(paused, request, "use_existing", registry=registry)
        with pytest.raises(UserInputResumeError) as exc:
            accept_confirmation(paused, request, "web_search", registry=registry)
    assert exc.value.code == "permission_recovery_decision_conflict"
    assert search.calls == 0


def test_medium_risk_healthy_call_waits_for_peer_refusal_settlement_without_disabling_guard(
    control, monkeypatch
):
    import asyncio
    from threading import Event, Lock

    from app.conversations.execution import ExecutionControl
    from app.schemas.tools import ToolCallResult
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_permission_fallback import DeniedTool

    second_guard = Event()
    lock, checks, remote_attempts = Lock(), [], []
    original_guard, original_run = ExecutionControl.ensure_tool_effects_resolved, DeniedTool.run

    def guard(self, **kwargs):
        if kwargs.get("db") is not None and kwargs["risk_level"] == "medium":
            with lock:
                checks.append(kwargs["call_id"])
                if len(checks) >= 2:
                    second_guard.set()
        return original_guard(self, **kwargs)

    async def run(self, input_data):
        with lock:
            remote_attempts.append(True)
            first = len(remote_attempts) == 1
        if first:
            assert await asyncio.to_thread(second_guard.wait, 3)
            return await original_run(self, input_data)
        self.calls += 1
        return ToolCallResult(
            tool_call_id="result",
            tool_name=self.name,
            status="success",
            trace_id="",
            result={"data": [1, 2, 3]},
        )

    monkeypatch.setattr(ExecutionControl, "ensure_tool_effects_resolved", guard)
    monkeypatch.setattr(DeniedTool, "risk_level", "medium")
    monkeypatch.setattr(DeniedTool, "run", run)
    graph, state, data, search, client, _ = batch_graph(control, monkeypatch)
    with execution_control(control):
        paused = graph.invoke(state, {"configurable": {"thread_id": "healthy-medium"}})
    assert data.calls == 2 and client.calls == 2 and search.calls == 0
    assert len(paused["__interrupt__"][0].value["recovery_batch"]["items"]) == 1
    assert sum(row["status"] == "success" for row in paused["task_results"]) == 2
    with control.session_factory() as db:
        receipts = list(
            db.scalars(
                select(AgentRunToolReceipt).where(AgentRunToolReceipt.run_id == control.run_id)
            )
        )
        assert len(receipts) == 2 and all(row.status == "settled" for row in receipts)


def test_budget_is_shared_with_prior_searches_and_consumed_in_stable_batch_order(
    control, monkeypatch
):
    from app.tests.unit.test_recovery_durability import search_request, success

    graph, state, _, search, _, registry = batch_graph(control, monkeypatch)
    for i in range(5):
        claim = control.claim_search_invocation(
            search_request(f"prior-{i}"),
            binding_digest=registry.get("web_search").execution_binding_digest,
            budget_limit=6,
            existing_call_ids=(),
        )
        control.tool_finished(claim.receipt_id, success())
    config = {"configurable": {"thread_id": "budget"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        resumed = graph.invoke(confirm(paused["__interrupt__"][0].value), config)
    assert search.calls == 1
    by_task = {item["task_id"]: item for item in resumed["task_results"]}
    assert by_task["cashflow"]["status"] == "success"
    assert by_task["income"]["error_code"] == "research_budget_exhausted"
    assert "__interrupt__" not in resumed


def test_unresolved_search_is_not_redispatched_and_other_authorized_item_continues(
    control, monkeypatch
):
    from app.graph.recovery.executor import recovery_search_request
    from app.schemas.recovery import RecoveryBatch

    graph, state, _, search, _, registry = batch_graph(control, monkeypatch)
    config = {"configurable": {"thread_id": "unresolved"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        batch = RecoveryBatch.model_validate(next(iter(paused["recovery_batches"].values())))
        request = recovery_search_request(batch=batch, item=batch.items[0])
        assert (
            control.claim_search_invocation(
                request,
                binding_digest=registry.get("web_search").execution_binding_digest,
                budget_limit=6,
                existing_call_ids=(),
            ).disposition
            == "execute"
        )
        resumed = graph.invoke(confirm(paused["__interrupt__"][0].value), config)
    assert search.calls == 1
    assert (
        next(row for row in resumed["task_results"] if row["task_id"] == batch.items[0].task_id)[
            "error_code"
        ]
        == "search_invocation_unresolved"
    )
    assert "__interrupt__" not in resumed


def test_one_search_failure_does_not_stop_other_authorized_items_or_reopen_confirmation(
    control, monkeypatch
):
    from app.schemas.tools import ToolCallResult, ToolError
    from app.tests.unit.test_permission_fallback import SearchTool

    original = SearchTool.run

    async def search_run(self, input_data):
        if self.calls == 0:
            self.calls += 1
            return ToolCallResult(
                tool_call_id="result",
                tool_name=self.name,
                status="failed",
                trace_id="",
                execution_state="completed",
                error=ToolError(code="web_search_no_results", message="无结果"),
            )
        return await original(self, input_data)

    monkeypatch.setattr(SearchTool, "run", search_run)
    graph, state, _, search, client, _ = batch_graph(control, monkeypatch)
    config = {"configurable": {"thread_id": "partial-failure"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        resumed = graph.invoke(confirm(paused["__interrupt__"][0].value), config)
    assert search.calls == client.calls == 2
    assert sum(row["status"] == "success" for row in resumed["task_results"]) == 2
    assert "__interrupt__" not in resumed and "planner_stop_reason" not in resumed


@pytest.mark.parametrize(
    "policy",
    [
        {"public_alternative": "unknown", "basis": []},
        {"public_alternative": "forbidden", "basis": ["test:user-source-constraint"]},
        {"public_alternative": "allowed", "basis": []},
    ],
)
def test_missing_or_forbidden_source_contract_leaves_gap_not_silent_search(
    control, monkeypatch, policy
):
    graph, state, _, search, _, _ = batch_graph(control, monkeypatch, policy=policy)
    with execution_control(control):
        finished = graph.invoke(state, {"configurable": {"thread_id": "unknown-source"}})
    assert "__interrupt__" not in finished and search.calls == 0
    assert [
        row["error_code"] for row in finished["task_results"] if row["status"] != "success"
    ] == ["source_policy_unavailable"] * 2


def test_recovery_uses_original_research_time_and_not_resume_date(control, monkeypatch):
    from app.tests.unit.test_permission_fallback import SearchTool

    contexts = []

    async def run_with_context(self, input_data, *, context):
        contexts.append(context)
        return await self.run(input_data)

    monkeypatch.setattr(SearchTool, "run_with_context", run_with_context, raising=False)
    graph, state, _, _, _, _ = batch_graph(control, monkeypatch)
    state["research_as_of"] = "2020-01-01T10:00:00+08:00"
    config = {"configurable": {"thread_id": "as-of"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        graph.invoke(confirm(paused["__interrupt__"][0].value), config)
    assert len(contexts) == 2
    assert all(context.research_context.as_of == state["research_as_of"] for context in contexts)


def test_production_resume_rejects_revocation_before_invoke(control, monkeypatch):
    from app.graph import runtime
    from app.graph.state import build_checkpoint_thread_id

    graph, state, _, search, _, registry = batch_graph(control, monkeypatch)
    thread_id = build_checkpoint_thread_id(
        tenant_id=state["tenant_id"],
        session_key=state["session_key"],
        request_id=state["request_id"],
    )
    config = {"configurable": {"thread_id": thread_id}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        interrupted = paused["__interrupt__"][0]
        payload = interrupted.value
        monkeypatch.setattr(runtime, "build_question_answer_graph", lambda **kwargs: graph)
        with pytest.raises(runtime.AgentGraphExecutionError) as exc:
            runtime.resume_user_input_graph(
                business_intent_catalog=object(),
                checkpointer=None,
                checkpoint_thread_id=thread_id,
                interrupt_id=interrupted.id,
                interaction_id=payload["interaction_id"],
                input_type="selection",
                value="web_search",
                presentation_version=payload["recovery_batch"]["version"],
                batch_digest=payload["recovery_batch"]["digest"],
                permission_scopes=[],
                tool_registry_factory=lambda: registry,
            )
    assert (
        exc.value.http_status == 409 and exc.value.code == "permission_recovery_permission_revoked"
    )
    assert search.calls == 0


def test_existing_uncertain_effect_remains_protected_and_is_not_rewritten(control, monkeypatch):
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_permission_fallback import DeniedTool

    monkeypatch.setattr(DeniedTool, "risk_level", "medium")
    receipt = control.tool_started(
        ToolCallRequest(
            call_id="controlled-unknown",
            tool_name="mcp.test.write",
            arguments={},
            purpose="受控未决执行",
        ),
        risk_level="medium",
    )
    control.tool_finished(
        receipt, {"status": "unknown", "error": {"code": "controlled_timeout"}}, uncertain=True
    )
    with control.session_factory() as db:
        before = dict(db.get(AgentRunToolReceipt, receipt).result_summary)
    graph, state, data, search, client, _ = batch_graph(control, monkeypatch)
    with execution_control(control):
        finished = graph.invoke(state, {"configurable": {"thread_id": "real-unknown"}})
    assert data.calls == search.calls == 0 and client.calls == 2
    assert "__interrupt__" not in finished
    assert [
        row["error_code"] for row in finished["task_results"] if row["status"] != "success"
    ] == ["tool_side_effects_unresolved"] * 2
    with control.session_factory() as db:
        original = db.get(AgentRunToolReceipt, receipt)
        assert original.status == "unknown" and original.result_summary == before


def test_new_task_has_new_batch_and_cannot_inherit_the_old_confirmation(control, monkeypatch):
    from app.graph.planning.contracts import PlanModel
    from app.graph.planning.policy import observation_signature

    graph, state, data, search, client, _ = batch_graph(control, monkeypatch)
    config = {"configurable": {"thread_id": "later"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        first = paused["__interrupt__"][0].value
        finished = graph.invoke(confirm(first), config)
        new_plan = {
            "tasks": [*state["plan"]["tasks"], {"id": "later", "task_goal": "查询新的数据"}]
        }
        next_pause = graph.invoke(
            {
                **finished,
                "plan": new_plan,
                "planner_decision": {"action": "continue"},
                "planner_reviewed_signature": observation_signature(
                    PlanModel.model_validate(new_plan), finished["task_results"]
                ),
            },
            config,
        )
    second = next_pause["__interrupt__"][0].value
    assert first["interaction_id"] != second["interaction_id"]
    assert first["recovery_batch"]["batch_id"] != second["recovery_batch"]["batch_id"]
    assert (
        len(first["recovery_batch"]["items"]) == 2 and len(second["recovery_batch"]["items"]) == 1
    )
    assert data.calls == client.calls == 3 and search.calls == 2
