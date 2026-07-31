from app.tools import default_registry


def test_mcp_server_env_uses_explicit_allowlist(monkeypatch):
    monkeypatch.setattr(
        default_registry.settings,
        "mcp_server_env_passthrough",
        ["TUSHARE_TOKEN", "MCP_MARKET_DATA_ADMIN_BASE_URL"],
    )
    monkeypatch.setenv("TUSHARE_TOKEN", "secret-token")
    monkeypatch.setenv("MCP_MARKET_DATA_ADMIN_BASE_URL", "http://admin")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")

    env = default_registry._mcp_server_env()

    assert env == {
        "TUSHARE_TOKEN": "secret-token",
        "MCP_MARKET_DATA_ADMIN_BASE_URL": "http://admin",
    }


def test_mcp_server_env_includes_custom_tushare_token_env_var(monkeypatch):
    monkeypatch.setattr(
        default_registry.settings,
        "mcp_server_env_passthrough",
        ["MCP_TUSHARE_TOKEN_ENV_VAR"],
    )
    monkeypatch.setenv("MCP_TUSHARE_TOKEN_ENV_VAR", "CUSTOM_TUSHARE_TOKEN")
    monkeypatch.setenv("CUSTOM_TUSHARE_TOKEN", "secret-token")

    env = default_registry._mcp_server_env()

    assert env == {
        "MCP_TUSHARE_TOKEN_ENV_VAR": "CUSTOM_TUSHARE_TOKEN",
        "CUSTOM_TUSHARE_TOKEN": "secret-token",
    }


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
