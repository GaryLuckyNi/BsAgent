"""模型输入的凭证安全副本；不修改控制状态，不把资料当作授权。"""

from functools import lru_cache

from app.security.context_content import ContextContentRejected, ContextContentUnavailable
from app.security.prompt_injection import detect_prompt_injection, system_boundary
from app.security.tool_content import ToolContentPolicy
from app.tools.redaction import _is_sensitive_key


@lru_cache(maxsize=1)
def _input_policy():
    return ToolContentPolicy(
        max_string_chars=1_000_000, max_total_chars=2_000_000, max_nodes=50_000,
        scan_injection=False,
    )


def model_input_view(value):
    failed = False
    try:
        view = _input_policy().project(value, tool_name="model-input", sink="workflow")
    except Exception:  # noqa: BLE001 - 在 except 外抛固定错误，不保留原文异常链。
        failed = True
    if failed:
        raise ContextContentUnavailable
    if view.metadata.disposition == "withheld":
        raise ContextContentRejected
    return view.data


def protect_model_prompts(system_prompt, user_prompt):
    if not isinstance(system_prompt, str) or not isinstance(user_prompt, str):
        raise ContextContentRejected
    view = model_input_view({"system_prompt": system_prompt, "user_prompt": user_prompt})
    return system_boundary(view["system_prompt"]), view["user_prompt"]


def check_protocol_identifier(value):
    if value is not None and (not isinstance(value, str) or model_input_view(value) != value):
        # 调用 ID 不能静默改写；否则消息配对、审批指纹会失去对应关系。
        raise ContextContentRejected


def check_tool_definitions(tools):
    """schema 字段名是契约，不能按业务数据键脱敏；发现秘密则拒绝绑定。"""
    nodes, chars = [0], [0]

    def walk(value, depth=0, field_name=None, active=frozenset()):
        nodes[0] += 1
        if nodes[0] > 50_000 or depth > 32:
            raise ContextContentRejected
        if isinstance(value, str):
            chars[0] += len(value)
            if chars[0] > 2_000_000 or model_input_view(value) != value:
                raise ContextContentRejected
            if detect_prompt_injection(value):
                raise ContextContentRejected
            return
        if value is None or isinstance(value, (bool, int, float)):
            if model_input_view(value) != value:
                raise ContextContentRejected
            return
        if not isinstance(value, (dict, list, tuple)) or id(value) in active:
            raise ContextContentRejected
        active = active | {id(value)}
        if isinstance(value, dict):
            for key, item in value.items():
                check_protocol_identifier(key)
                chars[0] += len(key)
                if chars[0] > 2_000_000:
                    raise ContextContentRejected
                if (
                    key in {"default", "const", "enum", "examples"}
                    and model_input_view(item) != item
                ):
                    raise ContextContentRejected
                if (
                    field_name
                    and _is_sensitive_key(field_name)
                    and key in {"default", "const", "enum", "examples"}
                ):
                    raise ContextContentRejected
                if key == "properties" and isinstance(item, dict):
                    for name, schema in item.items():
                        check_protocol_identifier(name)
                        chars[0] += len(name)
                        if chars[0] > 2_000_000:
                            raise ContextContentRejected
                        walk(schema, depth + 2, name, active)
                else:
                    walk(item, depth + 1, field_name, active)
        else:
            for item in value:
                walk(item, depth + 1, field_name, active)

    walk(tools)


def check_model_options(options):
    for key, value in options.items():
        if key in {"tools", "functions"}:
            check_tool_definitions(value)
        elif model_input_view({key: value}) != {key: value}:
            raise ContextContentRejected
