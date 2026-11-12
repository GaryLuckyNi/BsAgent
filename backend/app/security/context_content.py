"""历史摘要与记忆的公共内容适配，不改原文、不产生权限。"""

from app.security.tool_content import ToolContentPolicy
from app.tools.redaction import contains_inline_credentials


class ContextContentRejected(RuntimeError):
    code = "context_content_rejected"

    def __init__(self):
        super().__init__("上下文内容受限。")


class ContextContentUnavailable(RuntimeError):
    code = "context_content_unavailable"

    def __init__(self):
        super().__init__("上下文内容检查暂不可用。")


def _inspect(value):
    failed = False
    try:
        view = ToolContentPolicy(injection_action="withhold").project(value, tool_name="context-content", sink="workflow")
    except Exception:  # noqa: BLE001 - 在 except 外抛固定错误，不保留原始异常链。
        failed = True
    if failed:
        raise ContextContentUnavailable
    return view


def protect_context_payload(value):
    view = _inspect(value)
    if view.metadata.disposition == "withheld":
        raise ContextContentRejected
    return view.data


def require_safe_context_content(value):
    view = _inspect(value)
    # 先通过有界检查，再执行已有补充判断，避免旧递归扫描超深输入。
    if view.metadata.disposition != "allowed" or contains_inline_credentials(view.data):
        raise ContextContentRejected


def context_content_is_safe(value):
    try:
        require_safe_context_content(value)
    except (ContextContentRejected, ContextContentUnavailable):
        return False
    return True
