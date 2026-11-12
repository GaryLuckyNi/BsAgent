"""受管观测出口的安全副本；检查故障只扣留正文，不影响业务副作用。"""

from app.security.tool_content import ToolContentPolicy


def observation_view(value):
    try:
        view = ToolContentPolicy(
            max_string_chars=1_000_000, max_total_chars=2_000_000, max_nodes=50_000,
            scan_injection=False,
        ).project(value, tool_name="observation", sink="log")
        if view.metadata.disposition != "withheld":
            return view.data
    except Exception:  # noqa: BLE001, S110 - 观测故障不能另行打印原文或抛回业务调用。
        pass
    if isinstance(value, dict):
        return {"content_withheld": True}
    if isinstance(value, (list, tuple)):
        return []
    if isinstance(value, str):
        return "[观测内容已扣留]"
    return None
