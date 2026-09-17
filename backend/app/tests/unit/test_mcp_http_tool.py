import asyncio

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from app.schemas.tools import ToolError
from app.tools import mcp_http
from app.tools.mcp_http import DynamicMcpHttpTool, McpHttpClient


def _langchain_tool(name: str, payload: object, *, schema: dict | None = None):
    async def call(**_kwargs):
        return (
            [{"type": "text", "text": "MCP structured response"}],
            {"structured_content": payload},
        )

    return StructuredTool.from_function(
        coroutine=call,
        name=name,
        description=name,
        args_schema=schema or {"type": "object", "properties": {}},
        response_format="content_and_artifact",
    )


def _dynamic_tool(name: str, payload: object) -> DynamicMcpHttpTool:
    return DynamicMcpHttpTool(
        name=f"mcp.local.{name}",
        server_name="local",
        langchain_tool=_langchain_tool(name, payload),
        description=name,
        input_schema={"type": "object", "properties": {}},
    )


def test_http_client_uses_official_streamable_http_adapter(monkeypatch):
    captured = {}
    discovered = _langchain_tool(
        "echo",
        {"text": "ok"},
        schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )

    class FakeMultiServerMCPClient:
        def __init__(self, connections, **options):
            captured["connections"] = connections
            captured["options"] = options

        async def get_tools(self, *, server_name):
            captured["server_name"] = server_name
            return [discovered]

    monkeypatch.setattr(mcp_http, "MultiServerMCPClient", FakeMultiServerMCPClient)

    tools = asyncio.run(
        McpHttpClient(url="https://mcp.internal/mcp", timeout_seconds=12.5).list_tools(
            server_name="local"
        )
    )

    assert captured == {
        "connections": {
            "local": {
                "transport": "streamable_http",
                "url": "https://mcp.internal/mcp",
                "timeout": 12.5,
                "sse_read_timeout": 12.5,
            }
        },
        "options": {"handle_tool_errors": True},
        "server_name": "local",
    }
    assert tools[0].name == "mcp.local.echo"
    assert tools[0].spec.version == "mcp-http-v1"
    assert tools[0].spec.input_schema["required"] == ["text"]


def test_dynamic_http_tool_reads_official_structured_artifact():
    tool = _dynamic_tool("echo", {"text": "ok"})

    result = asyncio.run(tool.run(tool.input_model.model_validate({})))

    assert result.status == "success"
    assert result.result == {"text": "ok"}


def test_dynamic_http_tool_reads_fastmcp_json_text_fallback():
    response = ToolMessage(
        content=[{"type": "text", "text": '{"text":"ok"}'}],
        tool_call_id="call-1",
        status="success",
    )

    assert mcp_http._langchain_tool_payload(response) == {"text": "ok"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('[{"ts_code":"300085.SZ"}]', [{"ts_code": "300085.SZ"}]),
        ('"ok"', "ok"),
        ("0", 0),
        ("false", False),
        ("null", None),
    ],
)
def test_dynamic_http_tool_reads_any_json_text_value(text, expected):
    response = ToolMessage(
        content=[{"type": "text", "text": text}],
        tool_call_id="call-1",
        status="success",
    )

    assert mcp_http._langchain_tool_payload(response) == expected


def test_dynamic_http_tool_reads_structured_json_array_artifact():
    tool = _dynamic_tool("rows", [{"ts_code": "300085.SZ"}])

    result = asyncio.run(tool.run(tool.input_model.model_validate({})))

    assert result.status == "success"
    assert result.result == [{"ts_code": "300085.SZ"}]


def test_dynamic_http_tool_preserves_and_redacts_market_failure():
    tool = _dynamic_tool(
        "stock.market_snapshot",
        {
            "status": "failed",
            "error": {
                "code": "required_api_permission_missing",
                "message": "当前数据源权限无法提供必需字段。",
                "details": {
                    "missing_fields": ["pe_ttm"],
                    "token": "secret-token",
                    "endpoint": "postgresql://agent:db-pass@postgres/agent",
                },
                "retryable": False,
            },
        },
    )

    result = asyncio.run(tool.run(tool.input_model.model_validate({})))

    assert result.status == "failed"
    assert result.error == ToolError(
        code="required_api_permission_missing",
        message="当前数据源权限无法提供必需字段。",
        details={
            "missing_fields": ["pe_ttm"],
            "token": "***",
            "endpoint": "postgresql://***@postgres/agent",
        },
        retryable=False,
    )


def test_dynamic_http_tool_accepts_partial_market_envelope():
    tool = _dynamic_tool(
        "stock.market_snapshot",
        {
            "status": "partial",
            "data": {"symbol": "300085.SZ", "price": 10.5},
            "coverage": {
                "status": "partial",
                "missing_optional_fields": ["pe_ttm"],
            },
        },
    )

    result = asyncio.run(tool.run(tool.input_model.model_validate({})))

    assert result.status == "success"
    assert result.result["status"] == "partial"


def test_dynamic_http_tool_rejects_malformed_market_envelope():
    tool = _dynamic_tool(
        "stock.market_snapshot",
        {"status": "unknown", "raw": "Bearer secret-value"},
    )

    result = asyncio.run(tool.run(tool.input_model.model_validate({})))

    assert result.status == "failed"
    assert result.error.code == "mcp_tool_response_invalid"
    assert result.error.details == {}


def test_dynamic_http_tool_normalizes_adapter_errors_without_leaking(monkeypatch):
    tool = _dynamic_tool("echo", {"text": "unused"})

    async def failed_invoke(_input):
        raise RuntimeError("Authorization: Bearer top-secret")

    monkeypatch.setattr(type(tool._langchain_tool), "ainvoke", lambda self, value: failed_invoke(value))

    result = asyncio.run(tool.run(tool.input_model.model_validate({})))

    assert result.status == "failed"
    assert result.error.code == "mcp_tool_execution_failed"
    assert "top-secret" not in result.error.message
