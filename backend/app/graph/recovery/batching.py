"""在派发前固定成员，在 superstep 收敛后封存授权范围。"""

from collections.abc import Callable, Sequence

from app.graph.planning.contracts import SubTaskModel, task_snapshot_matches
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
# 纳入冻结条目摘要的本地阻塞说明；保留既有权限批次的呈现，兼容已保存的中断。
LOCAL_BLOCKED_SOURCE_CONSTRAINT = (
    "历史工具执行仍未核实，本次原接口未派发；确认仅允许补充公开资料，不核销历史未决记录，原接口数据仍缺失。"
)


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
                reason=("历史工具执行尚未核实，本次原接口未派发。"
                        if item.source_constraint == LOCAL_BLOCKED_SOURCE_CONSTRAINT
                        else "数据源账号权限不足，原接口未执行。"),
                source_notice=(f"{SOURCE_NOTICE}{item.source_constraint}"
                               if item.source_constraint == LOCAL_BLOCKED_SOURCE_CONSTRAINT
                               else SOURCE_NOTICE),
            )
            for item in batch.items
        ),
    )


def batch_request(batch: RecoveryBatch) -> UserInputRequest:
    return UserInputRequest(
        interaction_id="interaction_"
        + snapshot_digest([batch.version, batch.batch_id, batch.digest])[:32],
        input_type="selection",
        question=("以下任务因先前工具执行尚未核实等原因未取得原数据，是否改用联网搜索补充公开资料？"
                  if any(item.source_constraint == LOCAL_BLOCKED_SOURCE_CONSTRAINT for item in batch.items)
                  else "以下任务的数据源账号权限不足，是否改用联网搜索补充公开资料？"),
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
        # 已形成恢复事项的原调用已经执行并结算，不能再当作未派发等待项。
        if (deferred["task_id"] in completed or call_id in state.get("recovery_items", {})
                or deferred.get("status") != "waiting"):
            continue
        context = state["tool_selection_contexts"][call_id]
        record = records[call_id]
        if (
            context.get("request_id") != state["request_id"]
            or not task_snapshot_matches(context.get("task"), tasks.get(deferred["task_id"]))
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
    # 中高风险副作用保护只容许一个未结算调用；等待项一起重派会再次竞争，
    # 将仍未远端开始的调用误记为最终失败。每次只放行一个，其他项保留原参数继续等待。
    return sends[:1]


def settle_blocked_deferred(state, registry):
    from app.conversations.execution import UnresolvedToolEffect
    from app.graph.recovery.validation import make_recovery_item, require_control

    if not state.get("deferred_tool_calls"):
        return {}
    control = require_control(state)
    completed = {row["task_id"] for row in state.get("task_results", [])}
    results = []
    recovery_items = {}
    records = {row.request.call_id: row for row in state.get("tool_calls", [])}
    for call_id, deferred in sorted(state.get("deferred_tool_calls", {}).items()):
        if (deferred["task_id"] in completed or call_id in state.get("recovery_items", {})
                or deferred.get("status") != "waiting"):
            continue
        record = records[call_id]
        try:
            # superstep 已结束；仍有 running/unknown 时保留保护，不等同于健康同批等待。
            control.ensure_tool_effects_resolved(
                risk_level=registry.get(record.request.tool_name).spec.risk_level, call_id=call_id
            )
        except UnresolvedToolEffect:
            context = state.get("tool_selection_contexts", {}).get(call_id)
            task_data = next((task for task in (state.get("plan") or {}).get("tasks", [])
                              if task["id"] == deferred["task_id"]), None)
            if context and task_data is not None and record.result is not None:
                item, _ = make_recovery_item(
                    state, SubTaskModel.model_validate(task_data), record.request,
                    record.result, context, registry,
                )
                if item is not None:
                    recovery_items[call_id] = item.model_dump(mode="json")
                    continue
            results.append(
                {
                    "task_id": deferred["task_id"],
                    "status": "business_failure",
                    "content": "存在尚未核实的工具执行，未派发本项调用；请核实已有结果。",
                    "error_code": "tool_side_effects_unresolved",
                    "tool_name": record.request.tool_name,
                }
            )
    return {
        **({"task_results": results} if results else {}),
        **({"recovery_items": recovery_items} if recovery_items else {}),
    }
