"""候选ID不是授权；终态审批必须绑定原主体和完整审批内容。"""

import hashlib
import json

from app.memory.sensitivity import detect_sensitivity


class MemoryCandidateConflict(RuntimeError):
    code = "memory_candidate_conflict"
    http_status = 409
    node_name = "memory"
    message = "记忆候选归属或审批内容不一致，请重新发起任务。"

    def __init__(self):
        super().__init__(self.message)


def candidate_body(candidate):
    # 与memory_write审批指纹使用同一组业务字段；不包含可变审批状态。
    return {
        "memory_type": candidate.memory_type,
        "memory_key": candidate.memory_key or candidate.candidate_id,
        "content": candidate.content,
        "confidence": candidate.confidence,
        "sensitivity": candidate.sensitivity,
        "source": candidate.source,
    }


def candidate_identity(candidate, *, tenant_id, user_id):
    payload = {
        "version": 2,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "body": candidate_body(candidate),
    }
    return (
        "memcand_v2_"
        + hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
    )


def require_candidate_binding(existing, candidate, *, tenant_id, user_id):
    if (existing.tenant_id, existing.user_id) != (tenant_id, user_id):
        raise MemoryCandidateConflict()
    if existing.memory_type != candidate.memory_type:
        raise MemoryCandidateConflict()
    if existing.approval_status in {"approved", "rejected"} and candidate_body(
        existing
    ) != candidate_body(candidate):
        raise MemoryCandidateConflict()


def memory_approval_required(candidate):
    if candidate.approval_status == "approved":
        return False
    return candidate.sensitivity in {"medium", "high"} or any(
        detect_sensitivity(value) in {"medium", "high"}
        for value in (candidate.content, candidate.memory_key, candidate.source or "")
    )
