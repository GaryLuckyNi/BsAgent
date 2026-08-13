from app.core.config import settings
from app.search.factory import build_web_search_client
from app.schemas.tools import ToolSpec
from app.tools.catalog_runtime import ToolCatalogRuntime
from app.tools.mcp_gateway import McpGateway, McpServerConfig
from app.tools.registry import ToolRegistry
from app.tools.web_search import WebSearchTool


def _discover_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    gateway = McpGateway()
    server_config = McpServerConfig(
        name=settings.mcp_server_name,
        url=settings.mcp_server_url,
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


_default_tool_catalog = ToolCatalogRuntime(
    registry_factory=_discover_default_tool_registry,
)


def get_default_tool_registry() -> ToolRegistry:
    return _default_tool_catalog.snapshot()


def get_default_tool_catalog_runtime() -> ToolCatalogRuntime:
    return _default_tool_catalog


def _clear_default_tool_registry() -> None:
    _default_tool_catalog.close()


get_default_tool_registry.cache_clear = _clear_default_tool_registry  # type: ignore[attr-defined]


def list_default_tool_specs() -> list[ToolSpec]:
    return get_default_tool_registry().specs()
