# BsAgent MCP Server

## 目标

这个目录提供一个真实 MCP stdio server，用于验证 Agent 通过 MCP 协议发现和调用工具的链路。

当前不再提供 HTTP mock API，也不暴露：

```text
GET  /api/mcp/manifest
POST /api/mcp/tools/{tool_name}/invoke
```

MCP client 应通过 stdio 启动服务并调用工具。

## 工具

当前内置两个 MCP tools：

- `echo`：返回输入文本。
- `calculate_sum`：计算数字列表总和。

`calculate_sum` 输入示例：

```json
{
  "numbers": [1, 2, 3]
}
```

返回内容：

```json
{
  "total": 6,
  "count": 3
}
```

## 启动

stdio MCP server 通常由 MCP client 作为子进程启动：

```bash
cd "mcp server"
/opt/anaconda3/bin/python3 -m app.main
```

不要用浏览器或 curl 访问它；stdio server 会通过标准输入/输出交换 MCP JSON-RPC 消息。

## 测试

```bash
cd "mcp server"
/opt/anaconda3/bin/python3 -m pytest tests -v
```

测试使用官方 MCP Python client：

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
```

并验证：

```text
list_tools -> echo, calculate_sum
call_tool("echo")
call_tool("calculate_sum")
```

## BsAgent 接入

BsAgent 侧通过 `backend/app/tools/mcp_stdio.py` 接入真实 MCP stdio server：

```text
ToolCallRequest(name="mcp.mock.calculate_sum")
-> McpCalculateSumTool
-> 启动 "mcp server" 子进程
-> ClientSession.call_tool("calculate_sum")
-> ToolCallResult
```

MCP 不应该作为顶层 intent。顶层 intent 仍是 `tool_action`，MCP 是 tool/capability provider。
