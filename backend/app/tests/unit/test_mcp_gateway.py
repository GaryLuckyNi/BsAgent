from types import SimpleNamespace

from app.schemas.tools import ToolSpec
from app.tools.mcp_gateway import McpGateway, McpServerConfig, McpToolManifest


def test_mcp_gateway_converts_manifest_to_internal_tool_spec():
    gateway = McpGateway()

    gateway.register_manifest(
        server_name="filesystem",
        manifest=McpToolManifest(
            name="read_file",
            description="读取文件",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
    )

    spec = gateway.get_tool_spec("mcp.filesystem.read_file")

    assert spec.name == "mcp.filesystem.read_file"
    assert spec.description == "读取文件"
    assert spec.input_schema["required"] == ["path"]
    assert spec.permission_scope == ["mcp:filesystem"]
    assert spec.risk_level == "medium"


def test_mcp_gateway_discovers_tools_through_configured_provider():
    calls = []
    discovered_spec = ToolSpec(
        name="mcp.stock.company_profile",
        version="mcp-http-v1",
        description="查询公司资料",
    )
    discovered_tools = [SimpleNamespace(spec=discovered_spec)]

    def discoverer(**kwargs):
        calls.append(kwargs)
        return discovered_tools

    gateway = McpGateway(discoverer=discoverer)
    config = McpServerConfig(
        name="stock",
        url="http://mcp-stock:8011/mcp",
        timeout_seconds=12.5,
        default_risk_level="medium",
    )

    tools = gateway.discover_tools(config)

    assert tools == discovered_tools
    assert gateway.list_tool_specs() == [discovered_spec]
    assert calls == [
        {
            "url": "http://mcp-stock:8011/mcp",
            "server_name": "stock",
            "timeout_seconds": 12.5,
            "risk_level": "medium",
            "permission_scope": ["mcp:stock"],
        }
    ]


def test_mcp_gateway_can_delegate_authorization_to_external_provider():
    calls = []

    def discoverer(**kwargs):
        calls.append(kwargs)
        return []

    gateway = McpGateway(discoverer=discoverer)
    config = McpServerConfig(
        name="external-stock",
        url="https://market-data.example/mcp",
        enforce_local_rbac=False,
    )

    gateway.discover_tools(config)

    assert calls[0]["permission_scope"] == []


def test_pre_execution_semantics_are_trusted_config_and_change_binding():
    calls = []
    gateway = McpGateway(discoverer=lambda **kwargs: calls.append(kwargs) or [])
    config = McpServerConfig(name="arbitrary", url="https://provider.example/mcp")
    assert config.permission_denial_is_pre_execution is False
    trusted = config.model_copy(update={"permission_denial_is_pre_execution": True})
    gateway.discover_tools(trusted)
    assert calls[0]["permission_denial_is_pre_execution"] is True
    tool = SimpleNamespace(name="mcp.arbitrary.new_api")
    assert gateway.execution_binding(config, tool=tool).digest != gateway.execution_binding(trusted, tool=tool).digest


def test_mcp_execution_binding_tracks_server_and_config_revisions():
    tool = SimpleNamespace(name="mcp.stock.company_profile")
    first = McpGateway.execution_binding(
        McpServerConfig(
            name="stock-a",
            url="http://mcp-a:8011/mcp",
            deployment_revision="deploy-1",
            config_revision="config-1",
        ),
        tool=tool,
    )
    second = McpGateway.execution_binding(
        McpServerConfig(
            name="stock-b",
            url="http://mcp-b:8011/mcp",
            deployment_revision="deploy-2",
            config_revision="config-2",
        ),
        tool=tool,
    )

    assert first.digest != second.digest
    assert "mcp-a" not in first.model_dump_json()


def test_mcp_execution_binding_tracks_effective_http_config_without_leaking_it():
    tool = SimpleNamespace(name="mcp.stock.company_profile")
    first = McpGateway.execution_binding(
        McpServerConfig(
            name="stock",
            url="https://mcp-first.internal/mcp",
            deployment_revision="deploy-1",
            config_revision="config-1",
        ),
        tool=tool,
    )
    second = McpGateway.execution_binding(
        McpServerConfig(
            name="stock",
            url="https://mcp-second.internal/mcp",
            deployment_revision="deploy-1",
            config_revision="config-1",
        ),
        tool=tool,
    )

    assert first.digest != second.digest
    serialized = first.model_dump_json()
    assert "mcp-first.internal" not in serialized


def test_mcp_execution_binding_tracks_local_authorization_policy():
    tool = SimpleNamespace(name="mcp.stock.company_profile")
    locally_authorized = McpGateway.execution_binding(
        McpServerConfig(
            name="stock",
            url="https://mcp.internal/mcp",
            enforce_local_rbac=True,
        ),
        tool=tool,
    )
    provider_authorized = McpGateway.execution_binding(
        McpServerConfig(
            name="stock",
            url="https://mcp.internal/mcp",
            enforce_local_rbac=False,
        ),
        tool=tool,
    )

    assert locally_authorized.digest != provider_authorized.digest


def test_mcp_execution_binding_identifies_streamable_http_transport():
    tool = SimpleNamespace(name="mcp.stock.company_profile")
    binding = McpGateway.execution_binding(
        McpServerConfig(name="stock", url="https://mcp.internal/mcp"),
        tool=tool,
    )

    assert binding.implementation_id == "mcp.http.mcp.stock.company_profile"
