from app.tools import default_registry


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
    default_registry.get_default_tool_registry.cache_clear()


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
    default_registry.get_default_tool_registry.cache_clear()


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
    default_registry.get_default_tool_registry.cache_clear()
