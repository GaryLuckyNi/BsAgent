from pathlib import Path
import json

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_stdio_server_lists_and_invokes_tools():
    server_root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command="/opt/anaconda3/bin/python3",
        args=["-m", "app.main"],
        cwd=str(server_root),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}

            assert {"echo", "calculate_sum"} <= tool_names

            echo_result = await session.call_tool("echo", {"text": "hello mcp"})
            assert json.loads(echo_result.content[0].text) == {"text": "hello mcp"}

            sum_result = await session.call_tool("calculate_sum", {"numbers": [1, 2, 3.5]})
            assert json.loads(sum_result.content[0].text) == {"total": 6.5, "count": 3}
