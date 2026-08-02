from functools import lru_cache
import os
from pathlib import Path

from app.core.config import settings
from app.search.factory import build_web_search_client
from app.schemas.tools import ToolSpec
from app.tools.mcp_gateway import McpGateway, McpServerConfig
from app.tools.registry import ToolRegistry
from app.tools.web_search import WebSearchTool


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def get_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    gateway = McpGateway()
    server_config = McpServerConfig(
        name=settings.mcp_server_name,
        command=settings.mcp_server_command,
        args=settings.mcp_server_args,
        cwd=_resolve_project_path(settings.mcp_server_cwd),
        env=_mcp_server_env(),
        timeout_seconds=settings.mcp_timeout_seconds,
        deployment_revision=settings.mcp_server_revision,
        config_revision=settings.mcp_config_revision,
    )
    tools = gateway.discover_tools(server_config)
    for tool in tools:
        registry.register(
            tool,
            execution_binding=gateway.execution_binding(server_config, tool=tool),
        )
    if settings.web_search_enabled and settings.deepseek_api_key.strip():
        registry.register(WebSearchTool(client=build_web_search_client()))
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


def _mcp_server_env() -> dict[str, str]:
    names = {name.strip() for name in settings.mcp_server_env_passthrough if name.strip()}
    token_env_var = os.getenv("MCP_TUSHARE_TOKEN_ENV_VAR", "").strip()
    if token_env_var:
        names.add(token_env_var)
    return {
        name: value
        for name in names
        if (value := os.getenv(name)) is not None and value.strip()
    }
