"""加载指定工具定义，不做语义排序或替代选择。"""

from app.schemas.tools import ToolSpec
from app.skills.workflow_contracts import WorkflowError
from app.tools.execution_gateway import has_required_scopes
from app.tools.registry import ToolRegistry


def resolve_named_tool(
    registry: ToolRegistry, name: str, *, permission_scopes: tuple[str, ...]
) -> ToolSpec:
    name = name.strip()
    if registry.has(name):
        candidates = [registry.get(name).spec]
    else:
        candidates = [spec for spec in registry.specs() if spec.name.endswith("." + name)]
    if not candidates:
        raise WorkflowError(f"指定工具不存在：{name}", "skill_tool_not_found")
    if len(candidates) != 1:
        raise WorkflowError(f"指定名称匹配多个工具，请使用完整名称：{name}", "skill_tool_ambiguous")
    spec = candidates[0]
    if spec.name == "search_tool":
        raise WorkflowError("固定 Skill 应直接声明业务工具，不允许通过 search_tool 绕过固定步骤。")
    if not has_required_scopes(granted=permission_scopes, required=spec.permission_scope):
        raise WorkflowError(f"无权调用指定工具：{spec.name}", "tool_permission_denied")
    return spec
