import asyncio
from pathlib import Path

from app.tools.mcp_stdio import (
    McpCalculateSumInput,
    McpCalculateSumTool,
    discover_mcp_stdio_tools,
)


def test_mcp_calculate_sum_tool_invokes_real_mcp_stdio_server():
    server_root = Path(__file__).resolve().parents[4] / "mcp server"

    async def run_tool():
        return await McpCalculateSumTool(
            command="/opt/anaconda3/bin/python3",
            args=["-m", "app.main"],
            cwd=str(server_root),
        ).run(McpCalculateSumInput(numbers=[1, 2, 3.5]))

    result = asyncio.run(run_tool())

    assert result.status == "success"
    assert result.tool_name == "mcp.mock.calculate_sum"
    assert result.result == {"total": 6.5, "count": 3}


def test_mcp_stdio_discovery_builds_dynamic_tools_from_list_tools():
    server_root = Path(__file__).resolve().parents[4] / "mcp server"

    tools = discover_mcp_stdio_tools(
        command="/opt/anaconda3/bin/python3",
        args=["-m", "app.main"],
        cwd=str(server_root),
        server_name="mock",
        timeout_seconds=30.0,
    )

    specs = {tool.name: tool.spec for tool in tools}

    assert {"mcp.mock.echo", "mcp.mock.calculate_sum"} <= set(specs)
    assert specs["mcp.mock.calculate_sum"].version == "mcp-stdio-v1"
    assert specs["mcp.mock.calculate_sum"].input_schema["required"] == ["numbers"]
    assert specs["mcp.mock.echo"].input_schema["required"] == ["text"]


def test_dynamic_mcp_stdio_tool_executes_discovered_calculate_sum_tool():
    server_root = Path(__file__).resolve().parents[4] / "mcp server"
    tools = discover_mcp_stdio_tools(
        command="/opt/anaconda3/bin/python3",
        args=["-m", "app.main"],
        cwd=str(server_root),
        server_name="mock",
        timeout_seconds=30.0,
    )
    tool = next(item for item in tools if item.name == "mcp.mock.calculate_sum")

    result = asyncio.run(
        tool.run(tool.input_model.model_validate({"numbers": [1, 2, 3]}))
    )

    assert result.status == "success"
    assert result.tool_name == "mcp.mock.calculate_sum"
    assert result.result == {"total": 6, "count": 3}


def test_mcp_stdio_discovery_includes_a_share_stock_tools():
    server_root = Path(__file__).resolve().parents[4] / "mcp server"

    tools = discover_mcp_stdio_tools(
        command="/opt/anaconda3/bin/python3",
        args=["-m", "app.main"],
        cwd=str(server_root),
        server_name="mock",
        timeout_seconds=30.0,
    )
    specs = {tool.name: tool.spec for tool in tools}

    assert "mcp.mock.stock.resolve_company" in specs
    assert "mcp.mock.stock.company_profile" in specs
    assert "mcp.mock.stock.financial_metrics" in specs
    assert "mcp.mock.stock.market_snapshot" in specs
    assert "mcp.mock.stock.announcement_search" in specs
    assert "mcp.mock.stock.risk_events" in specs
    assert specs["mcp.mock.stock.resolve_company"].input_schema["required"] == ["query"]


def test_dynamic_mcp_stdio_tool_executes_discovered_resolve_company_tool():
    server_root = Path(__file__).resolve().parents[4] / "mcp server"
    tools = discover_mcp_stdio_tools(
        command="/opt/anaconda3/bin/python3",
        args=["-m", "app.main"],
        cwd=str(server_root),
        server_name="mock",
        timeout_seconds=30.0,
    )
    tool = next(item for item in tools if item.name == "mcp.mock.stock.resolve_company")

    result = asyncio.run(tool.run(tool.input_model.model_validate({"query": "贵州茅台"})))

    assert result.status == "success"
    assert result.result["symbol"] == "600519.SH"
    assert result.result["name"] == "贵州茅台"
    assert result.result["source_provider"] == "fixture"
