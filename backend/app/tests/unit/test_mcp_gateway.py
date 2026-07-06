from app.tools.mcp_gateway import McpGateway, McpToolManifest


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
