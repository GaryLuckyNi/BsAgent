"""仅依赖服务端快照与独立回执；客户端不提供任务授权范围。"""

from sqlalchemy import select

from app.conversations.execution import current_execution
from app.graph.nodes.user_input_gate import UserInputResumeError
from app.graph.planning.contracts import SubTaskModel
from app.graph.recovery.batching import batch_request, run_identity
from app.graph.recovery.policy import eligible_recovery, seal_batch
from app.graph.recovery.source_policy import resolve_source_policy
from app.operations.langfuse_scheduler import record_recovery_event
from app.schemas.recovery import RECOVERY_VERSION, RecoveryBatch, RecoveryItem, snapshot_digest
from app.schemas.tools import ToolCallResult
from app.storage.models import AgentMessage, AgentRun, AgentRunToolReceipt
from app.tools.execution_gateway import has_required_scopes
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.runtime_capabilities import arguments_fingerprint


def invalid(code="permission_recovery_snapshot_mismatch"):
    return UserInputResumeError("权限恢复快照或当前授权已变化，请重新发起请求。", code=code)


def require_control(state):
    control = current_execution.get()
    if control is None:
        raise invalid("permission_recovery_context_required")
    with control.session_factory() as db:
        control.guard(db)
        run = db.get(AgentRun, control.run_id)
        if run is None or any(
            getattr(run, key) != state[key]
            for key in (
                "tenant_id",
                "user_id",
                "session_id",
                "session_key",
            )
        ):
            raise invalid("permission_recovery_identity_mismatch")
        message = db.scalar(
            select(AgentMessage).where(
                AgentMessage.run_id == control.run_id, AgentMessage.role == "assistant"
            )
        )
        if (
            message is None
            or message.request_id != state["request_id"]
            or message.message_id != state["assistant_message_id"]
        ):
            raise invalid("permission_recovery_identity_mismatch")
    return control


def settled_original(state, request, result, registry) -> bool:
    control = require_control(state)
    with control.session_factory() as db:
        control.guard(db)
        rows = list(
            db.scalars(
                select(AgentRunToolReceipt).where(
                    AgentRunToolReceipt.run_id == control.run_id,
                    AgentRunToolReceipt.call_id == request.call_id,
                )
            )
        )
        if len(rows) != 1 or rows[0].status != "settled" or rows[0].tool_name != request.tool_name:
            return False
        summary = rows[0].result_summary
        if not isinstance(summary, dict):
            return False
        entry = registry.get(request.tool_name, request.tool_version)
        metadata = summary.get("_capability_scope", {})
        if not isinstance(metadata, dict):
            return False
        scope = metadata.get("scope", {})
        if not isinstance(scope, dict) or any(
            scope.get(key) != state[key]
            for key in (
                "tenant_id",
                "user_id",
                "request_id",
            )
        ):
            return False
        if (
            scope.get("binding_digest") != entry.execution_binding_digest
            or scope.get("tool_version") != entry.spec.version
            or scope.get("account_revision") != state.get("account_revision")
            or metadata.get("arguments_fingerprint")
            != arguments_fingerprint(request, tool_version=entry.spec.version)
        ):
            return False
        return ToolCallResult.model_validate(summary) == result


def source_allows_search(selection) -> bool:
    if not selection:
        return False
    policy = selection.get("source_policy", {}) if selection else {}
    if not isinstance(policy, dict):
        return False
    basis = policy.get("basis")
    return bool(
        not selection.get("fixed_skill")
        and policy.get("public_alternative") == "allowed"
        and isinstance(basis, list)
        and basis
        and all(isinstance(ref, str) and ref.strip() for ref in basis)
    )


def make_recovery_item(state, task, request, result, selection, registry):
    eligible, reason = eligible_recovery(
        request=request,
        result=result,
        receipt_settled=settled_original(state, request, result, registry),
        selection=selection,
        source_allows_search=(source_allows_search(selection)
            and selection["source_policy"] == resolve_source_policy(state, task)),
        consumed=request.call_id in state.get("recovery_outcomes", {}),
    )
    if not eligible:
        return None, reason
    if not selection.get("dispatch_batch_id") or not selection.get("as_of"):
        return None, "recovery_dispatch_unavailable"
    fallback = next(item for item in selection["candidates"] if item["name"] == "web_search")
    try:
        item = RecoveryItem(
            task_id=task.id,
            original_call_id=request.call_id,
            selection_digest=snapshot_digest(selection),
            task_goal=task.task_goal,
            completion_criteria=tuple(task.completion_criteria),
            original_tool=request.tool_name,
            fallback_version=fallback["version"],
            fallback_binding=fallback["binding"],
            source_constraint="仅允许经确认补充公开资料；原接口数据仍缺失，不代表原验收条件已满足。",
        )
    except ValueError:
        return None, "task_contract_too_large"
    return item, reason


def validate_batch(state, request, *, registry) -> RecoveryBatch:
    try:
        return _validate_batch(state, request, registry=registry)
    except UserInputResumeError as exc:
        record_recovery_event(
            "recovery_invalidated",
            request_id=state.get("request_id"),
            batch_id=getattr(getattr(request, "recovery_batch", None), "batch_id", None),
            route_reason=exc.code,
        )
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        record_recovery_event(
            "recovery_invalidated",
            request_id=state.get("request_id"),
            route_reason="permission_recovery_snapshot_mismatch",
        )
        raise invalid() from exc


def _validate_batch(state, request, *, registry) -> RecoveryBatch:
    if state.get("recovery_policy_version") != RECOVERY_VERSION or request.recovery_batch is None:
        raise invalid("permission_recovery_version_mismatch")
    require_control(state)
    raw = state.get("recovery_batches", {}).get(request.recovery_batch.batch_id)
    if raw is None:
        raise invalid()
    batch = RecoveryBatch.model_validate(raw)
    expected = seal_batch(
        run_identity=run_identity(state),
        dispatch_batch_id=batch.dispatch_batch_id,
        items=batch.items,
    )
    if expected != batch or batch_request(batch) != request:
        raise invalid()
    dispatch = state.get("dispatch_batches", {}).get(batch.dispatch_batch_id, {})
    body = {key: value for key, value in dispatch.items() if key != "dispatch_batch_id"}
    if (
        dispatch.get("run_identity") != batch.run_identity
        or "dispatch_" + snapshot_digest(body)[:32] != batch.dispatch_batch_id
    ):
        raise invalid()
    contexts = state.get("tool_selection_contexts", {})
    records = {row.request.call_id: row for row in state.get("tool_calls", [])}
    tasks = {
        row["id"]: SubTaskModel.model_validate(row)
        for row in (state.get("plan") or {}).get("tasks", [])
    }
    for item in batch.items:
        context, record, task = (
            contexts.get(item.original_call_id),
            records.get(item.original_call_id),
            tasks.get(item.task_id),
        )
        if (
            not context
            or record is None
            or record.result is None
            or task is None
            or context.get("request_id") != state["request_id"]
            or context.get("call_id") != item.original_call_id
            or context.get("dispatch_batch_id") != batch.dispatch_batch_id
            or item.task_id not in dispatch.get("members", [])
            or snapshot_digest(context) != item.selection_digest
            or context.get("request_fingerprint") != tool_call_fingerprint(record.request)
            or context.get("task") != task.model_dump(mode="json")
            or task.task_goal != item.task_goal
            or tuple(task.completion_criteria) != item.completion_criteria
            or not source_allows_search(context)
            or context.get("source_policy") != resolve_source_policy(state, task)
            or task.skill_name is not None
        ):
            raise invalid()
        # 封存后每个绑定都要仍然有效；账号修订已包含在 binding / 回执范围中。
        for candidate in context["candidates"]:
            if not registry.has(candidate["name"]):
                raise invalid()
            entry = registry.get(candidate["name"])
            if (
                entry.spec.version != candidate["version"]
                or entry.execution_binding_digest != candidate["binding"]
            ):
                raise invalid()
        candidate = next((row for row in context["candidates"] if row["name"] == "web_search"), {})
        if (
            candidate.get("version") != item.fallback_version
            or candidate.get("binding") != item.fallback_binding
            or item.original_tool != record.request.tool_name
        ):
            raise invalid()
        eligible, _ = eligible_recovery(
            request=record.request,
            result=record.result,
            receipt_settled=settled_original(state, record.request, record.result, registry),
            selection=context,
            source_allows_search=True,
            consumed=False,
        )
        if not eligible:
            raise invalid()
    if not has_required_scopes(
        granted=tuple(state["permission_scopes"]),
        required=registry.get("web_search").spec.permission_scope,
    ):
        raise invalid("permission_recovery_permission_revoked")
    return batch
