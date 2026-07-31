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
        version="mcp-stdio-v1",
        description="查询公司资料",
    )
    discovered_tools = [SimpleNamespace(spec=discovered_spec)]

    def discoverer(**kwargs):
        calls.append(kwargs)
        return discovered_tools

    gateway = McpGateway(discoverer=discoverer)
    config = McpServerConfig(
        name="stock",
        command="python",
        args=["-m", "app.main"],
        cwd="/srv/mcp-stock",
        env={"TUSHARE_TOKEN": "secret-token"},
        timeout_seconds=12.5,
        default_risk_level="medium",
    )

    tools = gateway.discover_tools(config)

    assert tools == discovered_tools
    assert gateway.list_tool_specs() == [discovered_spec]
    assert calls == [
        {
            "command": "python",
            "args": ["-m", "app.main"],
            "cwd": "/srv/mcp-stock",
            "env": {"TUSHARE_TOKEN": "secret-token"},
            "server_name": "stock",
            "timeout_seconds": 12.5,
            "risk_level": "medium",
        }
    ]


def test_mcp_execution_binding_tracks_server_and_config_revisions():
    tool = SimpleNamespace(name="mcp.stock.company_profile")
    first = McpGateway.execution_binding(
        McpServerConfig(
            name="stock-a",
            command="python",
            deployment_revision="deploy-1",
            config_revision="config-1",
        ),
        tool=tool,
    )
    second = McpGateway.execution_binding(
        McpServerConfig(
            name="stock-b",
            command="python",
            deployment_revision="deploy-2",
            config_revision="config-2",
        ),
        tool=tool,
    )

    assert first.digest != second.digest
    assert "python" not in first.model_dump_json()


def test_mcp_execution_binding_tracks_effective_process_config_without_leaking_it():
    tool = SimpleNamespace(name="mcp.stock.company_profile")
    first = McpGateway.execution_binding(
        McpServerConfig(
            name="stock",
            command="python",
            args=["-m", "server.first"],
            cwd="/srv/mcp-first",
            env={"TUSHARE_TOKEN": "secret-token"},
            deployment_revision="deploy-1",
            config_revision="config-1",
        ),
        tool=tool,
    )
    second = McpGateway.execution_binding(
        McpServerConfig(
            name="stock",
            command="uv",
            args=["run", "server.second"],
            cwd="/srv/mcp-second",
            deployment_revision="deploy-1",
            config_revision="config-1",
        ),
        tool=tool,
    )

    assert first.digest != second.digest
    serialized = first.model_dump_json()
    assert "server.first" not in serialized
    assert "/srv/mcp-first" not in serialized
    assert "secret-token" not in serialized


def test_mcp_execution_binding_tracks_env_keys_without_leaking_values():
    tool = SimpleNamespace(name="mcp.stock.company_profile")
    first = McpGateway.execution_binding(
        McpServerConfig(
            name="stock",
            command="python",
            env={"TUSHARE_TOKEN": "secret-token"},
        ),
        tool=tool,
    )
    second = McpGateway.execution_binding(
        McpServerConfig(
            name="stock",
            command="python",
            env={"OTHER_TOKEN": "secret-token"},
        ),
        tool=tool,
    )

    assert first.digest != second.digest
    serialized = first.model_dump_json()
    assert "TUSHARE_TOKEN" not in serialized
    assert "secret-token" not in serialized
