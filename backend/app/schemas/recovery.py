"""不可变权限恢复授权；仅保存任务契约和执行引用，不复制工具结果。"""

import json
from hashlib import sha256
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RECOVERY_VERSION = "permission-recovery-batch-v1"
RECOVERY_KIND = "provider_permission_recovery_batch"


def snapshot_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(encoded.encode()).hexdigest()


def search_invocation_id(*, batch_id: str, original_call_id: str) -> str:
    return (
        "call_" + snapshot_digest([RECOVERY_VERSION, batch_id, original_call_id, "web_search"])[:32]
    )


class RecoveryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=64)
    original_call_id: str = Field(min_length=1, max_length=128)
    selection_digest: str = Field(min_length=1, max_length=64)
    task_goal: str = Field(min_length=1, max_length=500)
    completion_criteria: tuple[Annotated[str, Field(min_length=1, max_length=500)], ...] = Field(
        max_length=8
    )
    original_tool: str = Field(min_length=1, max_length=255)
    fallback_version: str = Field(min_length=1, max_length=128)
    fallback_binding: str = Field(min_length=1, max_length=128)
    source_constraint: str = Field(min_length=1, max_length=240)


class RecoveryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["permission-recovery-batch-v1"] = RECOVERY_VERSION
    batch_id: str = Field(min_length=1, max_length=128)
    run_identity: str = Field(min_length=1, max_length=256)
    dispatch_batch_id: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    items: tuple[RecoveryItem, ...] = Field(min_length=1, max_length=12)


class RecoveryItemView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(min_length=1, max_length=128)
    task_goal: str = Field(min_length=1, max_length=500)
    original_tool: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=240)
    source_notice: str = Field(min_length=1, max_length=240)


class RecoveryBatchView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["permission-recovery-batch-v1"] = RECOVERY_VERSION
    batch_id: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    items: tuple[RecoveryItemView, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def reject_duplicate_items(self):
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("recovery_batch_duplicate_call")
        return self
