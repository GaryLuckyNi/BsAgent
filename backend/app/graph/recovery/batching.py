"""在派发前固定成员，在 superstep 收敛后封存授权范围。"""

from collections.abc import Callable, Sequence

from app.graph.planning.contracts import SubTaskModel
from app.graph.recovery.policy import seal_batch
from app.operations.langfuse_scheduler import record_recovery_event
from app.schemas.recovery import (
    RECOVERY_KIND,
    RECOVERY_VERSION,
    RecoveryBatch,
    RecoveryBatchView,
    RecoveryItem,
    RecoveryItemView,
    snapshot_digest,
)
from app.schemas.user_input import UserInputOption, UserInputRequest, UserInputSource

SOURCE_NOTICE = "联网搜索为替代来源，未经原接口验证；原接口数据仍缺失。"


def run_identity(state) -> str:
    return snapshot_digest(
        [state[key] for key in ("tenant_id", "user_id", "session_key", "request_id")]
    )


def dispatch_snapshot(state, tasks: Sequence[SubTaskModel]) -> dict:
    body = {
        "run_identity": run_identity(state),
        "plan_digest": snapshot_digest(state.get("plan")),
        "observations_digest": snapshot_digest(
            [
                [item["task_id"], item["status"], snapshot_digest(item.get("content", ""))]
                for item in state.get("task_results", [])
            ]
        ),
        "members": sorted(task.id for task in tasks),
    }
    return {"dispatch_batch_id": "dispatch_" + snapshot_digest(body)[:32], **body}


def capture_dispatch(node: Callable) -> Callable:
    def wrapped(state):
        update = node(state)
        batch = state.get("active_dispatch_batch")
        if state.get("recovery_policy_version") == RECOVERY_VERSION and batch:
            update = {
                **update,
                "dispatch_batches": {
                    **update.get("dispatch_batches", {}),
                    batch["dispatch_batch_id"]: batch,
                },
            }
        return update

    return wrapped


def batch_view(batch: RecoveryBatch) -> RecoveryBatchView:
    return RecoveryBatchView(
        batch_id=batch.batch_id,
        digest=batch.digest,
        items=tuple(
            RecoveryItemView(
                item_id=item.original_call_id,
                task_goal=item.task_goal,
                original_tool=item.original_tool,
                reason="数据源账号权限不足，原接口未执行。",
                source_notice=SOURCE_NOTICE,
            )
            for item in batch.items
        ),
    )


def batch_request(batch: RecoveryBatch) -> UserInputRequest:
    return UserInputRequest(
        interaction_id="interaction_"
        + snapshot_digest([batch.version, batch.batch_id, batch.digest])[:32],
        input_type="selection",
        question="以下任务的数据源账号权限不足，是否改用联网搜索补充公开资料？",
        options=(
            UserInputOption(
                value="web_search",
                label="对以上任务使用联网搜索",
                description="只对清单中的任务替代取证；公开来源不等价于原接口数据。",
            ),
            UserInputOption(
                value="use_existing",
                label="仅使用已有资料",
                description="不搜索，保留原接口数据缺口。",
            ),
        ),
        source=UserInputSource(node="task_scheduler", kind=RECOVERY_KIND),
        recovery_batch=batch_view(batch),
    )


def pending_batch_update(state) -> dict:
    if state.get("pending_user_input") is not None or state.get("user_input_response") is not None:
        return {}
    completed = {row["task_id"] for row in state.get("task_results", [])}
    contexts = state.get("tool_selection_contexts", {})
    outcomes = state.get("recovery_outcomes", {})
    for dispatch_id, dispatch in sorted(state.get("dispatch_batches", {}).items()):
        if dispatch.get("run_identity") != run_identity(state):
            raise ValueError("recovery_dispatch_identity_mismatch")
        items = [
            RecoveryItem.model_validate(raw)
            for call_id, raw in state.get("recovery_items", {}).items()
            if call_id not in outcomes
            and contexts.get(call_id, {}).get("dispatch_batch_id") == dispatch_id
        ]
        if not items:
            continue
        members = set(dispatch["members"])
        recoverable = {item.task_id for item in items}
        if not recoverable <= members:
            raise ValueError("recovery_dispatch_members_mismatch")
        if not members <= (completed | recoverable):
            # 未收敛不得按“目前看到的失败”缩小或提前授权。
            continue
        batch = seal_batch(
            run_identity=run_identity(state), dispatch_batch_id=dispatch_id, items=items
        )
        existing = state.get("recovery_batches", {}).get(batch.batch_id)
        if existing is not None and existing != batch.model_dump(mode="json"):
            raise ValueError("recovery_batch_changed")
        if existing is None:
            record_recovery_event(
                "recovery_batch_sealed",
                request_id=state["request_id"],
                batch_id=batch.batch_id,
                route_reason="dispatch_members_converged",
            )
        return {
            "recovery_batches": {batch.batch_id: batch.model_dump(mode="json")},
            "pending_user_input": batch_request(batch),
        }
    return {}


def has_remote_receipt(state, call_id):
    from sqlalchemy import select

    from app.graph.recovery.validation import require_control
    from app.storage.models import AgentRunToolReceipt

    control = require_control(state)
    with control.session_factory() as db:
        control.guard(db)
        return (
            db.scalar(
                select(AgentRunToolReceipt.receipt_id)
                .where(
                    AgentRunToolReceipt.run_id == control.run_id,
                    AgentRunToolReceipt.call_id == call_id,
                )
                .limit(1)
            )
            is not None
        )


def deferred_sends(state):
    """只派发从未远端开始、原参数仍一致的本地等待；不重试已有远端回执。"""
    from langgraph.types import Send

    from app.graph.recovery.validation import invalid
    from app.tools.fingerprints import tool_call_fingerprint

    completed = {row["task_id"] for row in state.get("task_results", [])}
    sends = []
    records = {row.request.call_id: row for row in state.get("tool_calls", [])}
    tasks = {
        row["id"]: SubTaskModel.model_validate(row).model_dump(mode="json")
        for row in (state.get("plan") or {}).get("tasks", [])
    }
    for call_id, deferred in sorted(state.get("deferred_tool_calls", {}).items()):
        if deferred["task_id"] in completed or deferred.get("status") != "waiting":
            continue
        context = state["tool_selection_contexts"][call_id]
        record = records[call_id]
        if (
            context.get("request_id") != state["request_id"]
            or context.get("task") != tasks.get(deferred["task_id"])
            or context.get("request_fingerprint") != tool_call_fingerprint(record.request)
            or has_remote_receipt(state, call_id)
        ):
            raise invalid()
        sends.append(
            Send(
                "tool_agent",
                {
                    **state,
                    "active_task": context["task"],
                    "active_skill_step": None,
                    "active_dispatch_batch": state["dispatch_batches"][
                        deferred["dispatch_batch_id"]
                    ],
                },
            )
        )
    return sends


def settle_blocked_deferred(state, registry):
    from app.conversations.execution import UnresolvedToolEffect
    from app.graph.recovery.validation import require_control

    if not state.get("deferred_tool_calls"):
        return {}
    control = require_control(state)
    completed = {row["task_id"] for row in state.get("task_results", [])}
    results = []
    records = {row.request.call_id: row for row in state.get("tool_calls", [])}
    for call_id, deferred in sorted(state.get("deferred_tool_calls", {}).items()):
        if deferred["task_id"] in completed or deferred.get("status") != "waiting":
            continue
        record = records[call_id]
        try:
            # superstep 已结束；仍有 running/unknown 时保留保护，不等同于健康同批等待。
            control.ensure_tool_effects_resolved(
                risk_level=registry.get(record.request.tool_name).spec.risk_level, call_id=call_id
            )
        except UnresolvedToolEffect:
            results.append(
                {
                    "task_id": deferred["task_id"],
                    "status": "business_failure",
                    "content": "存在尚未核实的工具执行，未派发本项调用；请核实已有结果。",
                    "error_code": "tool_side_effects_unresolved",
                    "tool_name": record.request.tool_name,
                }
            )
    return {"task_results": results} if results else {}
