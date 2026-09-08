import numpy as np
import pytest

from app.tools import default_registry
from app.tools.search import ToolSearchIndex, ToolSearchService


class _ZeroEmbedder:
    def embed_documents(self, texts):
        return np.zeros((len(texts), 2), dtype=np.float32)

    def embed_query(self, text):
        return np.zeros(2, dtype=np.float32)


@pytest.fixture(autouse=True)
def use_fast_test_search_service(monkeypatch):
    def build(registry):
        specs = [spec for spec in registry.specs() if spec.name != "search_tool"]
        index = ToolSearchIndex.build(
            specs=specs,
            catalog_digest=registry.catalog_digest,
            embedder=_ZeroEmbedder(),
        )
        return ToolSearchService(registry=registry, index=index, candidate_k=20)

    monkeypatch.setattr(default_registry, "_build_default_tool_search_service", build)
    default_registry.get_default_tool_registry.cache_clear()
    yield
    default_registry.get_default_tool_registry.cache_clear()


def test_default_registry_discovers_mcp_tools_from_configured_http_url(monkeypatch):
    captured = {}
    monkeypatch.setattr(default_registry.settings, "mcp_server_url", "http://mcp:8011/mcp")
    monkeypatch.setattr(default_registry.settings, "web_search_enabled", False)

    def discover_tools(self, config):
        captured["config"] = config
        return []

    monkeypatch.setattr(default_registry.McpGateway, "discover_tools", discover_tools)
    default_registry.get_default_tool_registry.cache_clear()

    default_registry.get_default_tool_registry()

    config = captured["config"]
    assert config.url == "http://mcp:8011/mcp"
    assert not hasattr(config, "command")
    assert not hasattr(config, "env")


def test_default_registry_registers_deepseek_web_search_as_platform_tool(monkeypatch):
    monkeypatch.setattr(default_registry.settings, "web_search_enabled", True)
    monkeypatch.setattr(default_registry.settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(default_registry.settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(
        default_registry.McpGateway,
        "discover_tools",
        lambda self, config: [],
    )
    default_registry.get_default_tool_registry.cache_clear()

    registry = default_registry.get_default_tool_registry()

    assert registry.has("web_search")
    spec = registry.get("web_search").spec
    assert spec.permission_scope == ["web:search"]
    assert spec.risk_level == "low"
    assert registry.has("search_tool")


def test_default_registry_omits_web_search_when_disabled(monkeypatch):
    monkeypatch.setattr(default_registry.settings, "web_search_enabled", False)
    monkeypatch.setattr(
        default_registry.McpGateway,
        "discover_tools",
        lambda self, config: [],
    )
    default_registry.get_default_tool_registry.cache_clear()

    registry = default_registry.get_default_tool_registry()

    assert not registry.has("web_search")
    assert registry.has("rag_search")
    assert registry.has("search_tool")
