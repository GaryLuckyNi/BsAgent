from pathlib import Path
import json

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app import main as server_main
from app.market_data.provider import MarketDataError
from app.market_data.schemas import MarketSnapshot, ToolCoverage


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

            assert "echo" in tool_names
            assert "calculate_sum" not in tool_names

            echo_result = await session.call_tool("echo", {"text": "hello mcp"})
            assert json.loads(echo_result.content[0].text) == {"text": "hello mcp"}

@pytest.mark.anyio
async def test_market_tools_publish_field_selection_schema_without_risk_events():
    server_root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command="/opt/anaconda3/bin/python3",
        args=["-m", "app.main"],
        cwd=str(server_root),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()

    tools = {tool.name: tool for tool in listed.tools}
    assert "stock.risk_events" not in tools
    assert {
        "stock.resolve_company",
        "stock.company_profile",
        "stock.market_snapshot",
        "stock.price_trend",
        "stock.financial_metrics",
        "stock.announcement_search",
    } <= set(tools)
    expected_fields = {
        "stock.resolve_company": {"symbol", "name", "exchange"},
        "stock.company_profile": {
            "name",
            "industry",
            "board",
            "main_business",
            "registered_address",
        },
        "stock.market_snapshot": {
            "price",
            "pct_change",
            "turnover",
            "market_cap",
            "pe_ttm",
            "pb",
        },
        "stock.price_trend": {
            "latest_price",
            "return_20d",
            "return_60d",
            "ma20",
            "ma60",
            "volatility_20d",
            "max_drawdown",
        },
        "stock.financial_metrics": {
            "revenue",
            "net_profit_parent",
            "operating_cash_flow",
            "revenue_yoy",
            "net_profit_yoy",
            "operating_cash_flow_yoy",
            "gross_margin",
            "roe",
            "debt_to_asset",
        },
        "stock.announcement_search": {"announcements"},
    }
    for tool_name, field_names in expected_fields.items():
        schema = tools[tool_name].inputSchema
        assert {"required_fields", "optional_fields"} <= set(schema["properties"])
        assert set(schema["properties"]["required_fields"]["items"]["enum"]) == field_names
        assert schema["properties"]["optional_fields"]["items"]["enum"] == schema[
            "properties"
        ]["required_fields"]["items"]["enum"]

    snapshot_schema = tools["stock.market_snapshot"].inputSchema
    assert snapshot_schema["required"] == ["symbol", "required_fields"]
    assert tools["stock.financial_metrics"].inputSchema["required"] == [
        "symbol",
        "period",
        "required_fields",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("required_fields", "optional_fields"),
    [([], []), (["price"], ["price"])],
    ids=["empty", "overlap"],
)
async def test_invalid_field_selection_returns_business_failure_and_keeps_session_usable(
    required_fields,
    optional_fields,
):
    server_root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command="/opt/anaconda3/bin/python3",
        args=["-m", "app.main"],
        cwd=str(server_root),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            failed = await session.call_tool(
                "stock.market_snapshot",
                {
                    "symbol": "300085.SZ",
                    "required_fields": required_fields,
                    "optional_fields": optional_fields,
                },
            )
            following = await session.call_tool(
                "echo",
                {"text": "still usable"},
            )

    assert failed.isError is False
    assert json.loads(failed.content[0].text) == {
        "status": "failed",
        "error": {
            "code": "invalid_field_request",
            "message": "字段选择参数无效。",
            "details": {"reason": "field_selection_invalid"},
            "retryable": False,
        },
    }
    assert following.isError is False
    assert json.loads(following.content[0].text) == {"text": "still usable"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {"symbol": "300085.SZ"},
        {
            "symbol": "300085.SZ",
            "required_fields": ["password=must-not-leak"],
        },
    ],
    ids=["missing-required-fields", "enum-invalid"],
)
async def test_protocol_argument_validation_fails_without_executing_provider_or_leaking(
    arguments,
):
    server_root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command="/opt/anaconda3/bin/python3",
        args=["-m", "app.main"],
        cwd=str(server_root),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            failed = await session.call_tool("stock.market_snapshot", arguments)
            following = await session.call_tool("echo", {"text": "still usable"})

    assert failed.isError is True
    assert following.isError is False
    assert json.loads(following.content[0].text) == {"text": "still usable"}


class _EnvelopeProvider:
    def __init__(self, *, status: str = "complete", error: MarketDataError | None = None):
        self.status = status
        self.error = error

    def get_market_snapshot(self, symbol, selection):
        if self.error is not None:
            raise self.error
        missing_optional = ["pe_ttm"] if self.status == "partial" else []
        provided = ["price"] if self.status == "partial" else ["price", "pe_ttm"]
        return MarketSnapshot(
            symbol=symbol,
            price=10.5,
            pe_ttm=None if self.status == "partial" else 12.3,
            source_provider="tushare",
            as_of_date="2026-07-27",
            coverage=ToolCoverage(
                requested_fields=["price", "pe_ttm"],
                required_fields=["price"],
                optional_fields=["pe_ttm"],
                provided_fields=provided,
                missing_optional_fields=missing_optional,
                source_apis=["daily"],
            ),
        )


@pytest.mark.parametrize("provider_status", ["complete", "partial"])
def test_market_tool_returns_success_envelope(monkeypatch, provider_status):
    monkeypatch.setattr(
        server_main,
        "market_data_provider",
        _EnvelopeProvider(status=provider_status),
    )

    result = server_main.stock_market_snapshot_tool(
        symbol="300085.SZ",
        required_fields=["price"],
        optional_fields=["pe_ttm"],
    )

    assert result["status"] == provider_status
    assert result["data"]["symbol"] == "300085.SZ"
    assert "coverage" not in result["data"]
    assert result["coverage"]["provided_fields"] == (
        ["price", "pe_ttm"] if provider_status == "complete" else ["price"]
    )


def test_market_tool_returns_structured_failure_envelope(monkeypatch):
    monkeypatch.setattr(
        server_main,
        "market_data_provider",
        _EnvelopeProvider(
            error=MarketDataError(
                "required_api_permission_missing",
                "当前数据源权限无法提供必需字段。",
                details={"missing_fields": ["pe_ttm"], "required_apis": ["daily_basic"]},
                retryable=False,
            )
        ),
    )

    result = server_main.stock_market_snapshot_tool(
        symbol="300085.SZ",
        required_fields=["pe_ttm"],
        optional_fields=[],
    )

    assert result == {
        "status": "failed",
        "error": {
            "code": "required_api_permission_missing",
            "message": "当前数据源权限无法提供必需字段。",
            "details": {
                "missing_fields": ["pe_ttm"],
                "required_apis": ["daily_basic"],
            },
            "retryable": False,
        },
    }
