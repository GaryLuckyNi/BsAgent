import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from mcp.types import Tool


def test_raw_mcp_output_contract_is_preserved(monkeypatch):
    from app.tools import mcp_http
    from app.tools.mcp_http import McpHttpClient

    output = {"type": "object", "properties": {"amount": {"type": "number", "description": "金额（元）"}}}
    raw = Tool(name="cashflow", description="现金流", inputSchema={"type": "object"}, outputSchema=output)
    class Client:
        def __init__(self, connections, **kwargs): self.connections = connections
        @asynccontextmanager
        async def session(self, name):
            async def list_tools(**kwargs): return SimpleNamespace(tools=[raw], nextCursor=None)
            yield SimpleNamespace(list_tools=list_tools)
    monkeypatch.setattr(mcp_http, "MultiServerMCPClient", Client)
    tools = asyncio.run(McpHttpClient(url="https://example.com/mcp").list_tools(server_name="public"))
    assert tools[0].spec.output_schema == output
    assert tools[0].spec.description == "现金流"
