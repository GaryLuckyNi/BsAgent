import logging
import re

from app.core.config import settings
from app.search.factory import build_web_search_client
from app.schemas.tools import ToolSpec
from app.tools.catalog_runtime import ToolCatalogRuntime
from app.tools.market_data_config import AdminMarketDataConfigClient
from app.tools.mcp_gateway import McpGateway, McpServerConfig
from app.tools.registry import ToolRegistry
from app.tools.rag_search import build_default_rag_search_registration
from app.tools.search import (
    SearchTool,
    ToolSearchIndex,
    ToolSearchService,
)
from app.tools.web_search import WebSearchTool


logger = logging.getLogger(__name__)
_HTTP_STATUS_PATTERN = re.compile(r"\b([45][0-9]{2})\b")


def _discover_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    rag_tool, rag_binding = build_default_rag_search_registration()
    registry.register(rag_tool, execution_binding=rag_binding)
    gateway = McpGateway()
    for server_config in _configured_mcp_servers():
        try:
            tools = gateway.discover_tools(server_config)
        except Exception as exc:  # noqa: BLE001 - 单个 MCP 失败不能阻断其他工具
            logger.warning(
                "mcp_tool_discovery_failed type=%s server=%s status=%s",
                type(exc).__name__,
                server_config.name,
                _http_status_code(exc) or "unknown",
            )
            continue
        for tool in tools:
            if registry.has(tool.spec.name):
                logger.warning(
                    "mcp_tool_name_conflict server=%s tool=%s",
                    server_config.name,
                    tool.spec.name,
                )
                continue
            registry.register(
                tool,
                execution_binding=gateway.execution_binding(server_config, tool=tool),
            )
    if settings.web_search_enabled and settings.deepseek_api_key.strip():
        registry.register(WebSearchTool(client=build_web_search_client()))
    return registry


def _load_market_data_mcp_config() -> McpServerConfig | None:
    return AdminMarketDataConfigClient(
        base_url=settings.admin_base_url,
        timeout_seconds=min(settings.mcp_timeout_seconds, 5.0),
    ).get_active_mcp_config()


def _configured_mcp_servers() -> list[McpServerConfig]:
    configs: list[McpServerConfig] = []
    try:
        market_data_config = _load_market_data_mcp_config()
    except Exception as exc:  # noqa: BLE001 - Admin 短暂失败时仍保留其他工具
        logger.warning(
            "market_data_mcp_config_failed type=%s",
            type(exc).__name__,
        )
    else:
        if market_data_config is not None:
            configs.append(market_data_config)

    generic_url = settings.mcp_server_url.strip()
    if generic_url:
        configs.append(
            McpServerConfig(
                name=settings.mcp_server_name.strip() or "agent-mcp",
                url=generic_url,
                timeout_seconds=settings.mcp_timeout_seconds,
                deployment_revision=settings.mcp_server_revision,
                config_revision=settings.mcp_config_revision,
            )
        )
    return configs


def _http_status_code(exc: Exception) -> int | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and 400 <= status_code <= 599:
            return status_code
        match = _HTTP_STATUS_PATTERN.search(str(current))
        if match is not None:
            return int(match.group(1))
        current = current.__cause__ or current.__context__
    return None


def _build_default_tool_search_service(registry: ToolRegistry) -> ToolSearchService:
    index = ToolSearchIndex.build(
        specs=[spec for spec in registry.specs() if spec.name != SearchTool.name],
        catalog_digest=registry.catalog_digest,
    )
    return ToolSearchService(
        registry=registry,
        index=index,
        candidate_k=settings.tool_search_candidate_k,
    )


_default_tool_catalog = ToolCatalogRuntime(
    registry_factory=_discover_default_tool_registry,
    search_service_factory=lambda registry: _build_default_tool_search_service(registry),
    refresh_seconds=settings.market_data_provider_refresh_seconds,
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
