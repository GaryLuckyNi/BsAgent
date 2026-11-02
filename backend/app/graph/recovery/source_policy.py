"""最小部署契约：只许可经确认的公开资料补充，不声明原接口等价。"""

from collections.abc import Mapping

from app.core import config
from app.graph.planning.contracts import SubTaskModel

SOURCE_POLICY_VERSION = "public-supplement-v1"


def configured_source_policy() -> dict[str, str]:
    return {"mode": config.settings.permission_recovery_source_policy, "version": SOURCE_POLICY_VERSION}


def resolve_source_policy(state: Mapping, task: SubTaskModel) -> dict:
    intent = state.get("intent") or {}
    constraint = intent.get("source_constraint") if isinstance(intent, Mapping) else None
    if state.get("active_skill_step") or task.skill_name:
        return {"public_alternative": "forbidden", "basis": ["constraint:fixed-skill"]}
    if constraint == "original_only":
        return {"public_alternative": "forbidden", "basis": ["constraint:original-source-required"]}
    required = {"mode": "public_supplement_only", "version": SOURCE_POLICY_VERSION}
    # 恢复只检查请求来源限制与部署契约；数据需求标签不授予或阻止取证。
    if (
        state.get("recovery_source_policy") != required
        or configured_source_policy() != required
        or constraint not in {"unrestricted", "confirmation_required"}
    ):
        return {"public_alternative": "unknown", "basis": []}
    return {
        "public_alternative": "allowed",
        # allowed 仅表示可以询问。保留原来源审计字段，兼容已封存的有效中断。
        "basis": ["runtime-contract:public-supplement-v1", f"intent:{constraint}", f"task:{task.source_requirement}"],
    }
