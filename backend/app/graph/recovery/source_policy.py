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
    # 模型仅标记适用性；它不能填写部署快照、allowed 或规则依据。旧 Run 不因配置放宽升级。
    if (
        state.get("recovery_source_policy") != required
        or configured_source_policy() != required
        or constraint not in {"unrestricted", "confirmation_required"}
        or task.source_requirement not in {"public_information", "original_data"}
    ):
        return {"public_alternative": "unknown", "basis": []}
    return {
        "public_alternative": "allowed",
        # allowed 仅表示可以询问；original_data 是验收要求，不是用户禁止替代。
        # 搜索仍须批次确认，且不能将公开补充升级成原始接口数据。
        "basis": ["runtime-contract:public-supplement-v1", f"intent:{constraint}", f"task:{task.source_requirement}"],
    }


def ordinary_search_blocked(state: Mapping, task: SubTaskModel) -> bool:
    """普通选择不能获得来源切换授权；恢复节点只执行经确认的封存任务。"""
    constraint = (state.get("intent") or {}).get("source_constraint")
    if constraint == "original_only" or task.source_requirement == "original_data":
        return True
    for record in state.get("tool_calls", []):
        if not record.request.tool_name.startswith("mcp.") or record.result is None:
            continue
        # 不能通过改任务 ID 把失败后的替代搜索洗成普通新查询。
        # 未知执行也不能因搜索是 low-risk 就获得绕过确认的路径。
        if record.result.status != "success" or constraint == "confirmation_required":
            return True
    return False
