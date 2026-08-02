import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.schemas.tools import ToolCallRequest, ToolCallResult, ToolError
from app.tools.base import Tool
from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
from app.tools.mcp_stdio import (
    DynamicMcpStdioTool,
    McpStdioClient,
    McpToolResponseError,
    _parse_mcp_tool_result,
    discover_mcp_stdio_tools,
)
from app.tools.registry import ToolRegistry


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

    assert "mcp.mock.echo" in specs
    assert "mcp.mock.calculate_sum" not in specs
    assert specs["mcp.mock.echo"].input_schema["required"] == ["text"]


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
    assert "mcp.mock.stock.risk_events" not in specs
    assert specs["mcp.mock.stock.resolve_company"].input_schema["required"] == [
        "query",
        "required_fields",
    ]
    assert set(
        specs["mcp.mock.stock.resolve_company"].input_schema["properties"][
            "required_fields"
        ]["items"]["enum"]
    ) == {"symbol", "name", "exchange"}


def test_dynamic_mcp_stdio_tool_preserves_market_failure_envelope(monkeypatch):
    tool = DynamicMcpStdioTool(
        name="mcp.mock.stock.market_snapshot",
        server_name="mock",
        mcp_tool_name="stock.market_snapshot",
        description="查询行情",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "required_fields": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["symbol", "required_fields"],
        },
        output_schema=None,
        command="python",
        args=[],
    )

    async def failed_call(_tool_name, _arguments):
        return {
            "status": "failed",
            "error": {
                "code": "required_api_permission_missing",
                "message": "当前数据源权限无法提供必需字段。",
                "details": {
                    "missing_fields": ["pe_ttm"],
                    "token": "secret-token",
                    "endpoint": "postgresql://agent:db-pass@postgres/agent",
                    "nested": ["Bearer abcdefghijklmnop"],
                },
                "retryable": False,
            },
        }

    monkeypatch.setattr(tool._client, "call_tool", failed_call)
    result = asyncio.run(
        tool.run(
            tool.input_model.model_validate(
                {"symbol": "300085.SZ", "required_fields": ["pe_ttm"]}
            )
        )
    )

    assert result.status == "failed"
    assert result.result is None
    assert result.error == ToolError(
        code="required_api_permission_missing",
        message="当前数据源权限无法提供必需字段。",
        details={
            "missing_fields": ["pe_ttm"],
            "token": "***",
            "endpoint": "postgresql://***@postgres/agent",
            "nested": ["Bearer ***"],
        },
        retryable=False,
    )


class _GatewayInput(BaseModel):
    symbol: str


class _FailedMarketTool(Tool[_GatewayInput]):
    name = "market_failure"
    version = "1.0.0"
    input_model = _GatewayInput

    async def run(self, input_data):
        return ToolCallResult(
            tool_call_id="inner",
            tool_name=self.name,
            status="failed",
            error=ToolError(
                code="required_api_permission_missing",
                message="untrusted upstream message",
                details={
                    "missing_fields": ["pe_ttm"],
                    "required_apis": ["daily_basic"],
                    "password": "db-secret",
                    "diagnostic": [
                        "Authorization: Bearer abcdefghijklmnop",
                        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.signaturepart123",
                    ],
                },
                retryable=False,
            ),
            trace_id="inner",
        )


def test_execution_gateway_keeps_safe_market_error_details_and_retryable():
    registry = ToolRegistry()
    registry.register(_FailedMarketTool())
    request = ToolCallRequest(
        tool_name="market_failure",
        arguments={"symbol": "300085.SZ"},
        purpose="查询 PE",
    )

    result = asyncio.run(
        ToolExecutionGateway(registry).execute(
            request,
            trace_id="trace-1",
            context=ToolExecutionContext(
                tenant_id="t1",
                user_id="u1",
                session_id="s1",
                allowed_tools=("market_failure",),
            ),
        )
    )

    assert result.status == "failed"
    assert result.error.code == "required_api_permission_missing"
    assert result.error.message == "当前数据源权限无法提供必需字段。"
    assert result.error.details == {
        "missing_fields": ["pe_ttm"],
        "required_apis": ["daily_basic"],
        "password": "***",
        "diagnostic": ["Authorization: ***", "***"],
    }
    assert result.error.retryable is False


def test_dynamic_mcp_stdio_tool_treats_partial_envelope_as_success(monkeypatch):
    tool = DynamicMcpStdioTool(
        name="mcp.mock.stock.market_snapshot",
        server_name="mock",
        mcp_tool_name="stock.market_snapshot",
        description="查询行情",
        input_schema={"type": "object"},
        output_schema=None,
        command="python",
        args=[],
    )

    async def partial_call(_tool_name, _arguments):
        return {
            "status": "partial",
            "data": {"symbol": "300085.SZ", "price": 10.5},
            "coverage": {
                "status": "partial",
                "missing_optional_fields": ["pe_ttm"],
            },
        }

    monkeypatch.setattr(tool._client, "call_tool", partial_call)
    result = asyncio.run(tool.run(tool.input_model.model_validate({})))

    assert result.status == "success"
    assert result.result["status"] == "partial"


@dataclass
class _TextBlock:
    text: str


class _McpResult:
    def __init__(self, *, structured=None, content=None, is_error=False):
        self.structuredContent = structured
        self.content = content or []
        self.isError = is_error


def test_parse_mcp_tool_result_prefers_structured_content():
    result = _McpResult(
        structured={"total": 6, "count": 3},
        content=[_TextBlock('{"ignored": true}')],
    )

    assert _parse_mcp_tool_result(result) == {"total": 6, "count": 3}


def test_parse_mcp_tool_result_supports_snake_case_structured_content():
    result = _McpResult(content=[_TextBlock('{"ignored": true}')])
    result.structured_content = {"total": 6, "count": 3}

    assert _parse_mcp_tool_result(result) == {"total": 6, "count": 3}


def test_parse_mcp_tool_result_finds_json_object_in_later_text_block():
    result = _McpResult(
        content=[_TextBlock("informational text"), _TextBlock('{"total": 6}')]
    )

    assert _parse_mcp_tool_result(result) == {"total": 6}


def test_parse_mcp_tool_result_rejects_empty_or_non_object_response_without_raw_text():
    for result in (
        _McpResult(content=[]),
        _McpResult(content=[_TextBlock("secret raw response")]),
        _McpResult(structured=["not-an-object"]),
    ):
        try:
            _parse_mcp_tool_result(result)
        except McpToolResponseError as exc:
            assert exc.code == "mcp_tool_response_invalid"
            assert "secret raw response" not in str(exc)
        else:
            raise AssertionError("invalid MCP response must fail")


def test_dynamic_mcp_stdio_tool_rejects_unknown_or_malformed_market_envelope(monkeypatch):
    tool = DynamicMcpStdioTool(
        name="mcp.mock.stock.market_snapshot",
        server_name="mock",
        mcp_tool_name="stock.market_snapshot",
        description="查询行情",
        input_schema={"type": "object"},
        output_schema=None,
        command="python",
        args=[],
    )

    async def assert_invalid(payload):
        async def call(_tool_name, _arguments):
            return payload

        monkeypatch.setattr(tool._client, "call_tool", call)
        result = await tool.run(tool.input_model.model_validate({}))
        assert result.status == "failed"
        assert result.error.code == "mcp_tool_response_invalid"
        assert result.error.details == {}

    asyncio.run(assert_invalid({"status": "unknown", "raw": "Bearer secret-value"}))
    asyncio.run(
        assert_invalid(
            {
                "status": "partial",
                "data": {},
                "coverage": {"status": "complete"},
            }
        )
    )
    asyncio.run(assert_invalid({"status": "failed", "error": {"code": "bad"}}))


def test_dynamic_mcp_stdio_tool_keeps_statusless_regular_payload_compatible(monkeypatch):
    tool = DynamicMcpStdioTool(
        name="mcp.mock.echo",
        server_name="mock",
        mcp_tool_name="echo",
        description="回显",
        input_schema={"type": "object"},
        output_schema=None,
        command="python",
        args=[],
    )

    async def call(_tool_name, _arguments):
        return {"text": "ok"}

    monkeypatch.setattr(tool._client, "call_tool", call)
    result = asyncio.run(tool.run(tool.input_model.model_validate({})))

    assert result.status == "success"
    assert result.result == {"text": "ok"}


def test_stock_tool_rejects_statusless_payload_but_regular_tool_accepts_it(monkeypatch):
    async def call(_tool_name, _arguments):
        return {"price": 10.5}

    stock_tool = DynamicMcpStdioTool(
        name="mcp.mock.stock.market_snapshot",
        server_name="mock",
        mcp_tool_name="stock.market_snapshot",
        description="行情快照",
        input_schema={"type": "object"},
        output_schema=None,
        command="python",
        args=[],
    )
    regular_tool = DynamicMcpStdioTool(
        name="mcp.mock.price_preview",
        server_name="mock",
        mcp_tool_name="price_preview",
        description="普通工具",
        input_schema={"type": "object"},
        output_schema=None,
        command="python",
        args=[],
    )
    monkeypatch.setattr(stock_tool._client, "call_tool", call)
    monkeypatch.setattr(regular_tool._client, "call_tool", call)

    stock_result = asyncio.run(stock_tool.run(stock_tool.input_model.model_validate({})))
    regular_result = asyncio.run(
        regular_tool.run(regular_tool.input_model.model_validate({}))
    )

    assert stock_result.status == "failed"
    assert stock_result.error.code == "mcp_tool_response_invalid"
    assert regular_result.status == "success"
    assert regular_result.result == {"price": 10.5}


def test_parse_mcp_protocol_error_never_exposes_raw_content():
    result = _McpResult(
        content=[_TextBlock("validation failed for password=top-secret")],
        is_error=True,
    )

    try:
        _parse_mcp_tool_result(result)
    except McpToolResponseError as exc:
        assert exc.code == "mcp_tool_execution_failed"
        assert "top-secret" not in str(exc)
    else:
        raise AssertionError("MCP protocol error must fail")


def test_parse_mcp_protocol_error_maps_reliable_invalid_params_code():
    result = _McpResult(
        structured={"error": {"code": -32602, "message": "password=top-secret"}},
        is_error=True,
    )

    with pytest.raises(McpToolResponseError) as exc_info:
        _parse_mcp_tool_result(result)

    assert exc_info.value.code == "invalid_field_request"
    assert "top-secret" not in str(exc_info.value)


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
def test_mcp_stdio_client_normalizes_protocol_argument_errors_without_leaking(arguments):
    server_root = Path(__file__).resolve().parents[4] / "mcp server"
    client = McpStdioClient(
        command="/opt/anaconda3/bin/python3",
        args=["-m", "app.main"],
        cwd=str(server_root),
    )

    with pytest.raises(McpToolResponseError) as exc_info:
        asyncio.run(client.call_tool("stock.market_snapshot", arguments))

    assert exc_info.value.code == "mcp_tool_execution_failed"
    assert "must-not-leak" not in str(exc_info.value)


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

    result = asyncio.run(
        tool.run(
            tool.input_model.model_validate(
                {"query": "贵州茅台", "required_fields": ["symbol", "name"]}
            )
        )
    )

    assert result.status == "failed"
    assert result.error.code == "market_data_source_not_configured"
