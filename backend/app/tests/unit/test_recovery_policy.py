import pytest
from app.graph.recovery.policy import eligible_recovery, seal_batch
from app.schemas.recovery import RecoveryItem, search_invocation_id
from app.schemas.tools import ToolCallRequest, ToolCallResult, ToolError
from pydantic import ValidationError


def item(**updates):
    values = {
        "task_id": "a",
        "original_call_id": "c1",
        "selection_digest": "s1",
        "task_goal": "查询成交",
        "completion_criteria": ("包含日期",),
        "original_tool": "mcp.x.a",
        "fallback_version": "1",
        "fallback_binding": "b1",
        "source_constraint": "允许公开替代来源",
    }
    return RecoveryItem(**(values | updates))


def test_batch_is_stable_and_does_not_expand_after_seal():
    a, b = item(), item(task_id="b", original_call_id="c2")
    first = seal_batch(run_identity="run-1", dispatch_batch_id="dispatch-1", items=(b, a))
    same = seal_batch(run_identity="run-1", dispatch_batch_id="dispatch-1", items=(a, b))
    later = seal_batch(run_identity="run-1", dispatch_batch_id="dispatch-2", items=(b,))
    assert first == same
    assert first.items == (a, b)
    assert first.batch_id != later.batch_id
    assert search_invocation_id(batch_id=first.batch_id, original_call_id="c1") != (
        search_invocation_id(batch_id=later.batch_id, original_call_id="c1")
    )
    with pytest.raises(ValidationError):
        first.digest = "other"


@pytest.mark.parametrize(
    "items",
    [
        (),
        (item(), item()),
        (item(), item(original_call_id="c2")),
        tuple(item(original_call_id=f"c{i}") for i in range(13)),
    ],
)
def test_invalid_batch_is_rejected_not_truncated(items):
    with pytest.raises(ValueError):
        seal_batch(run_identity="run", dispatch_batch_id="dispatch", items=items)


def test_oversized_task_contract_is_rejected_not_truncated():
    with pytest.raises(ValidationError):
        item(completion_criteria=("长" * 501,))


def recovery_inputs():
    return {
        "request": ToolCallRequest(call_id="c1", tool_name="mcp.x.a", arguments={}, purpose="取证"),
        "result": ToolCallResult(
            tool_call_id="tool_result_1",
            tool_name="mcp.x.a",
            status="denied",
            trace_id="trace",
            execution_state="not_started",
            error=ToolError(code="provider_permission_denied", message="权限不足"),
        ),
        "receipt_settled": True,
        "consumed": False,
        "source_allows_search": True,
        "selection": {"candidates": [{"name": "web_search", "version": "1", "binding": "b1"}]},
    }


def test_only_explicit_settled_permission_denial_with_original_candidate_is_eligible():
    assert eligible_recovery(**recovery_inputs()) == (True, "permission_recovery_eligible")


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"receipt_settled": False}, "receipt_not_settled"),
        ({"consumed": True}, "recovery_already_consumed"),
        ({"selection": {"candidates": []}}, "fallback_not_in_original_candidates"),
        ({"selection": None}, "fallback_not_in_original_candidates"),
        ({"source_allows_search": False}, "source_policy_unavailable"),
    ],
)
def test_recovery_eligibility_rejection_matrix(updates, reason):
    assert eligible_recovery(**(recovery_inputs() | updates)) == (False, reason)


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"execution_state": "uncertain"}, "execution_not_confirmed_unstarted"),
        ({"result": {"partial": True}}, "execution_not_confirmed_unstarted"),
        (
            {"error": ToolError(code="provider_rate_limited", message="限流")},
            "not_provider_permission_denial",
        ),
        (
            {"error": ToolError(code="tool_permission_denied", message="本地拒绝")},
            "not_provider_permission_denial",
        ),
    ],
)
def test_other_errors_and_partial_execution_cannot_recover(updates, reason):
    args = recovery_inputs()
    args["result"] = args["result"].model_copy(update=updates)
    assert eligible_recovery(**args) == (False, reason)


def test_mismatched_result_cannot_authorize_recovery():
    args = recovery_inputs()
    args["result"] = args["result"].model_copy(update={"tool_name": "mcp.other.tool"})
    assert eligible_recovery(**args) == (False, "original_call_mismatch")


@pytest.mark.parametrize("proven", [False, True])
def test_local_block_requires_separate_unstarted_proof_not_a_fake_settled_receipt(proven):
    args = recovery_inputs()
    args["receipt_settled"] = False
    args["result"] = args["result"].model_copy(update={
        "error": ToolError(code="tool_side_effects_unresolved", message="先前执行未核实"),
    })
    assert eligible_recovery(**args, local_unstarted=proven) == (
        (True, "local_unstarted_recovery_eligible") if proven
        else (False, "not_provider_permission_denial")
    )


def test_existing_permission_interrupt_presentation_remains_compatible():
    from app.graph.recovery.batching import SOURCE_NOTICE, batch_request

    batch = seal_batch(run_identity="old-run", dispatch_batch_id="old-dispatch", items=[item()])
    request = batch_request(batch)
    assert request.question == "以下任务的数据源账号权限不足，是否改用联网搜索补充公开资料？"
    assert request.recovery_batch.items[0].reason == "数据源账号权限不足，原接口未执行。"
    assert request.recovery_batch.items[0].source_notice == SOURCE_NOTICE


def test_scheduler_waits_for_all_actual_dispatch_members_before_sealing():
    from app.graph.planning.contracts import SubTaskModel
    from app.graph.recovery.batching import dispatch_snapshot, pending_batch_update
    from app.graph.state import create_initial_state

    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="查询资料"
    )
    tasks = [
        SubTaskModel(id="a", task_goal="查询成交"),
        SubTaskModel(id="healthy", task_goal="读取已有材料"),
    ]
    state["plan"] = {"tasks": [task.model_dump(mode="json") for task in tasks]}
    dispatch = dispatch_snapshot(state, tasks)
    dispatch_id = dispatch["dispatch_batch_id"]
    state["dispatch_batches"] = {dispatch_id: dispatch}
    state["tool_selection_contexts"] = {"c1": {"dispatch_batch_id": dispatch_id}}
    state["recovery_items"] = {"c1": item().model_dump(mode="json")}
    assert pending_batch_update(state) == {}
    state["task_results"] = [{"task_id": "healthy", "status": "success", "content": "已有结果"}]
    update = pending_batch_update(state)
    assert len(update["pending_user_input"].recovery_batch.items) == 1
    state.update(update)
    assert pending_batch_update(state) == {}
