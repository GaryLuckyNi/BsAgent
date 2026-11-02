"""规划与原生工具调用共用的模型输入契约。"""

from app.schemas.tools import ToolSpec
from app.tool_documentation.metadata import model_schema
from app.tools.redaction import (
    contains_inline_credentials,
    reject_inline_credentials,
    reject_server_injected_arguments,
)


def model_input_schema(spec: ToolSpec) -> dict:
    schema = model_schema(spec)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        blocked = set(spec.server_injected_fields)
        blocked.update(name for name in properties
                       if contains_inline_credentials({name: "placeholder-value"}))
        schema["properties"] = {name: prop for name, prop in properties.items() if name not in blocked}
        if isinstance(schema.get("required"), list):
            schema["required"] = [name for name in schema["required"] if name not in blocked]
    return schema


def planning_input_schema(spec: ToolSpec) -> dict | None:
    """无法完整、安全披露时省略契约，不裁剪嵌套语义或扩张模型可见范围。"""
    schema = model_input_schema(spec)
    try:
        reject_inline_credentials(schema)
        # 将 schema 视为资料递归检查，避免 $defs/组合分支遗留服务器字段。
        reject_server_injected_arguments(schema, schema={},
                                         server_injected_fields=spec.server_injected_fields)
    except ValueError:
        return None
    if _has_sensitive_annotation(schema):
        return None
    return schema


def _has_sensitive_annotation(value) -> bool:
    if isinstance(value, dict):
        return bool(value.get("writeOnly") or value.get("x-sensitive")) or any(
            _has_sensitive_annotation(nested) for nested in value.values())
    if isinstance(value, list):
        return any(_has_sensitive_annotation(nested) for nested in value)
    return False
