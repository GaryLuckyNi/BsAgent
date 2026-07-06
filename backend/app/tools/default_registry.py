from functools import lru_cache
from pathlib import Path

from app.core.config import settings
from app.schemas.tools import ToolSpec
from app.tools.mcp_stdio import discover_mcp_stdio_tools
from app.tools.registry import ToolRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def get_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    tools = discover_mcp_stdio_tools(
        command=settings.mcp_server_command,
        args=settings.mcp_server_args,
        cwd=_resolve_project_path(settings.mcp_server_cwd),
        server_name=settings.mcp_server_name,
        timeout_seconds=settings.mcp_timeout_seconds,
    )
    for tool in tools:
        registry.register(tool)
    return registry


def list_default_tool_specs() -> list[ToolSpec]:
    return get_default_tool_registry().specs()


def _resolve_project_path(path: str | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate)
    return str(PROJECT_ROOT / candidate)
