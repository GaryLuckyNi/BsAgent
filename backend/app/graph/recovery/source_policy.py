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
    if constraint == "original_only" or task.source_requirement == "original_data":
        return {"public_alternative": "forbidden", "basis": ["constraint:original-source-required"]}
    required = {"mode": "public_supplement_only", "version": SOURCE_POLICY_VERSION}
    # 模型仅标记适用性；它不能填写部署快照、allowed 或规则依据。旧 Run 不因配置放宽升级。
    if (
        state.get("recovery_source_policy") != required
        or configured_source_policy() != required
        or constraint != "unrestricted"
        or task.source_requirement != "public_information"
    ):
        return {"public_alternative": "unknown", "basis": []}
    return {
        "public_alternative": "allowed",
        "basis": ["runtime-contract:public-supplement-v1", "intent:unrestricted", "task:public_information"],
    }
