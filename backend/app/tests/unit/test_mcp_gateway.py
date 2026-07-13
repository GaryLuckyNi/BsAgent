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
            "server_name": "stock",
            "timeout_seconds": 12.5,
            "risk_level": "medium",
        }
    ]
