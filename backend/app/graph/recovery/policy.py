"""无 I/O 的权限恢复资格判断与授权快照封存。"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.schemas.recovery import RECOVERY_VERSION, RecoveryBatch, RecoveryItem, snapshot_digest
from app.schemas.tools import ToolCallRequest, ToolCallResult


def seal_batch(
    *,
    run_identity: str,
    dispatch_batch_id: str,
    items: Sequence[RecoveryItem],
) -> RecoveryBatch:
    ordered = tuple(sorted(items, key=lambda item: (item.task_id, item.original_call_id)))
    if not 1 <= len(ordered) <= 12:
        raise ValueError("recovery_batch_size_invalid")
    ids = [item.original_call_id for item in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("recovery_batch_duplicate_call")
    if len({item.task_id for item in ordered}) != len(ordered):
        raise ValueError("recovery_batch_duplicate_task")
    body = {
        "version": RECOVERY_VERSION,
        "run_identity": run_identity,
        "dispatch_batch_id": dispatch_batch_id,
        "items": [item.model_dump(mode="json") for item in ordered],
    }
    digest = snapshot_digest(body)
    return RecoveryBatch(
        batch_id="recovery_" + digest[:32],
        digest=digest,
        items=ordered,
        run_identity=run_identity,
        dispatch_batch_id=dispatch_batch_id,
    )


def eligible_recovery(
    *,
    request: ToolCallRequest,
    result: ToolCallResult,
    receipt_settled: bool,
    selection: Mapping[str, Any] | None,
    source_allows_search: bool,
    consumed: bool,
) -> tuple[bool, str]:
    # result.tool_call_id 在现有网关中是结果 ID，不是请求身份；原调用由 Record/回执绑定。
    if not request.call_id or request.tool_name != result.tool_name:
        return False, "original_call_mismatch"
    if (
        not request.tool_name.startswith("mcp.")
        or result.status not in {"denied", "failed"}
        or result.error is None
        or result.error.code != "provider_permission_denied"
    ):
        return False, "not_provider_permission_denial"
    if result.execution_state != "not_started" or result.result is not None:
        return False, "execution_not_confirmed_unstarted"
    if not receipt_settled:
        return False, "receipt_not_settled"
    if consumed:
        return False, "recovery_already_consumed"
    candidates = selection.get("candidates", ()) if selection else ()
    if not isinstance(candidates, (list, tuple)) or not any(
        isinstance(item, Mapping)
        and item.get("name") == "web_search"
        and item.get("version")
        and item.get("binding")
        for item in candidates
    ):
        return False, "fallback_not_in_original_candidates"
    if not source_allows_search:
        return False, "source_policy_unavailable"
    return True, "permission_recovery_eligible"
