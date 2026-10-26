from types import SimpleNamespace

import pytest
from app.schemas.tools import ToolSpec
from app.tools import default_registry
from app.tools.mcp_gateway import McpServerConfig
from app.tools.search import ToolSearchIndex, ToolSearchService


@pytest.fixture(autouse=True)
def use_fast_test_search_service(monkeypatch):
    def build(registry):
        specs = [spec for spec in registry.specs() if spec.name != "search_tool"]
        index = ToolSearchIndex.build(
            specs=specs,
            catalog_digest=registry.catalog_digest,
        )
        return ToolSearchService(registry=registry, index=index, candidate_k=20)

    monkeypatch.setattr(default_registry, "_build_default_tool_search_service", build)
    monkeypatch.setattr(default_registry, "_load_market_data_mcp_config", lambda: None)
    monkeypatch.setattr(default_registry.settings, "mcp_server_url", "")
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


def test_search_registration_freezes_actual_implementation_and_configuration(monkeypatch):
    from app.search.deepseek import DeepSeekWebSearchClient
    from app.search.deepseek_api import DeepSeekSearchApiClient

    monkeypatch.setattr(default_registry.settings, "web_search_enabled", True)
    monkeypatch.setattr(default_registry.settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(default_registry.settings, "web_search_provider", "deepseek_search_api")
    monkeypatch.setattr(default_registry.settings, "web_search_base_url", "https://search.example/v1")
    monkeypatch.setattr(default_registry.settings, "web_search_timeout_seconds", 45.0)
    monkeypatch.setattr(default_registry.McpGateway, "discover_tools", lambda self, config: [])

    def entry():
        default_registry.get_default_tool_registry.cache_clear()
        return default_registry.get_default_tool_registry().get("web_search")

    direct = entry()
    assert isinstance(direct.executor._client, DeepSeekSearchApiClient)
    assert direct.execution_binding.provider == "local"
    assert "DeepSeekSearchApiClient" in direct.execution_binding.implementation_id
    assert direct.spec.version == "2.0.0" and direct.spec.timeout_ms == 45_000
    assert "test-key" not in direct.execution_binding.model_dump_json()
    monkeypatch.setattr(default_registry.settings, "deepseek_api_key", "rotated-key")
    assert entry().execution_binding_digest == direct.execution_binding_digest
    monkeypatch.setattr(default_registry.settings, "web_search_base_url", "https://new.example")
    assert entry().execution_binding_digest != direct.execution_binding_digest
    monkeypatch.setattr(default_registry.settings, "web_search_provider", "deepseek")
    legacy = entry()
    assert isinstance(legacy.executor._client, DeepSeekWebSearchClient)
    assert legacy.execution_binding_digest != direct.execution_binding_digest


def test_default_registry_omits_search_when_key_missing(monkeypatch, caplog):
    monkeypatch.setattr(default_registry.settings, "web_search_enabled", True)
    monkeypatch.setattr(default_registry.settings, "deepseek_api_key", " ")
    assert not default_registry.get_default_tool_registry().has("web_search")
    assert "web_search_not_configured" in caplog.text


def test_default_registry_keeps_platform_tools_when_mcp_discovery_fails(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(default_registry.settings, "web_search_enabled", True)
    monkeypatch.setattr(default_registry.settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(
        default_registry.settings,
        "mcp_server_url",
        "https://mcp.example/token=secret",
    )

    def discover_tools(self, config):
        raise RuntimeError(f"401 Unauthorized: {config.url}")

    monkeypatch.setattr(default_registry.McpGateway, "discover_tools", discover_tools)
    default_registry.get_default_tool_registry.cache_clear()

    registry = default_registry.get_default_tool_registry()

    assert registry.has("rag_search")
    assert registry.has("web_search")
    assert registry.has("search_tool")
    assert "mcp_tool_discovery_failed" in caplog.text
    assert "secret" not in caplog.text


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


def test_default_registry_discovers_admin_tushare_and_optional_generic_mcp(monkeypatch):
    monkeypatch.setattr(
        default_registry,
        "_load_market_data_mcp_config",
        lambda: McpServerConfig(
            name="tushare",
            url="https://api.tushare.pro/mcp/token=secret",
            config_revision="revision-1",
        ),
    )
    monkeypatch.setattr(
        default_registry.settings,
        "mcp_server_url",
        "https://agent-mcp.test/mcp",
    )
    monkeypatch.setattr(default_registry.settings, "mcp_server_name", "agent-mcp")
    names = []
    monkeypatch.setattr(
        default_registry.McpGateway,
        "discover_tools",
        lambda self, config: names.append(config.name) or [],
    )

    default_registry.get_default_tool_registry.cache_clear()
    default_registry.get_default_tool_registry()

    assert names == ["tushare", "agent-mcp"]


def test_default_registry_skips_empty_generic_mcp(monkeypatch):
    monkeypatch.setattr(default_registry.settings, "mcp_server_url", "")

    def fail_if_called(self, config):
        raise AssertionError(f"不应发现 MCP: {config.name}")

    monkeypatch.setattr(default_registry.McpGateway, "discover_tools", fail_if_called)

    registry = default_registry.get_default_tool_registry()

    assert registry.has("rag_search")


def test_default_registry_keeps_other_connections_when_one_discovery_fails(
    monkeypatch,
    caplog,
):
    generic_tool = SimpleNamespace(
        name="mcp.agent_mcp.lookup",
        spec=ToolSpec(
            name="mcp.agent_mcp.lookup",
            version="mcp-http-v1",
            description="查询通用 MCP 数据",
        ),
    )
    monkeypatch.setattr(
        default_registry,
        "_load_market_data_mcp_config",
        lambda: McpServerConfig(
            name="tushare",
            url="https://api.tushare.pro/mcp/token=secret",
        ),
    )
    monkeypatch.setattr(
        default_registry.settings,
        "mcp_server_url",
        "https://agent-mcp.test/mcp",
    )
    monkeypatch.setattr(default_registry.settings, "mcp_server_name", "agent-mcp")

    def discover_tools(self, config):
        if config.name == "tushare":
            raise RuntimeError(f"401 Unauthorized: {config.url}")
        return [generic_tool]

    monkeypatch.setattr(default_registry.McpGateway, "discover_tools", discover_tools)

    registry = default_registry.get_default_tool_registry()

    assert registry.has("mcp.agent_mcp.lookup")
    assert registry.has("rag_search")
    assert "mcp_tool_discovery_failed" in caplog.text
    assert "status=401" in caplog.text
    assert "secret" not in caplog.text
