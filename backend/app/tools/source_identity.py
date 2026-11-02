"""来源身份由宿主绑定决定；旧本地测试/注册兼容 MCP 命名空间。"""


def source_identity(name, binding=None):
    if binding is not None and binding.server_id:
        return f"{binding.provider}:{binding.server_id}"
    parts = name.split(".", 2)
    if len(parts) == 3 and parts[0] == "mcp":
        return f"mcp:{parts[1]}"
    return name
