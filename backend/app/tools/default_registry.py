from app.core.config import settings
from app.search.factory import build_web_search_client
from app.schemas.tools import ToolSpec
from app.tools.catalog_runtime import ToolCatalogRuntime
from app.tools.mcp_gateway import McpGateway, McpServerConfig
from app.tools.registry import ToolRegistry
from app.tools.rag_search import build_default_rag_search_registration
from app.tools.search import (
    CachingToolTextEmbedder,
    FastEmbedToolTextEmbedder,
    SearchTool,
    ToolSearchIndex,
    ToolSearchService,
)
from app.tools.web_search import WebSearchTool


def _discover_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    rag_tool, rag_binding = build_default_rag_search_registration()
    registry.register(rag_tool, execution_binding=rag_binding)
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


def _build_default_tool_search_service(registry: ToolRegistry) -> ToolSearchService:
    embedder = CachingToolTextEmbedder(
        FastEmbedToolTextEmbedder(
            model_name=settings.tool_search_embedding_model,
            cache_dir=settings.tool_search_embedding_cache_dir,
            local_files_only=settings.tool_search_local_files_only,
        ),
        catalog_digest=registry.catalog_digest,
        max_entries=settings.tool_search_query_cache_size,
    )
    index = ToolSearchIndex.build(
        specs=[spec for spec in registry.specs() if spec.name != SearchTool.name],
        catalog_digest=registry.catalog_digest,
        embedder=embedder,
    )
    return ToolSearchService(
        registry=registry,
        index=index,
        candidate_k=settings.tool_search_candidate_k,
    )


_default_tool_catalog = ToolCatalogRuntime(
    registry_factory=_discover_default_tool_registry,
    search_service_factory=lambda registry: _build_default_tool_search_service(registry),
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
