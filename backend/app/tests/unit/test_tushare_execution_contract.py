"""TuShare 受信只读契约、错误码与写入门禁的边界。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from app.tests.unit.test_mcp_http_tool import _langchain_tool
from app.tools.market_data_config import AdminMarketDataConfigClient
from app.tools.mcp_gateway import McpGateway, McpServerConfig
from app.tools.mcp_http import DynamicMcpHttpTool, McpHttpClient
from langchain_core.messages import ToolMessage
from mcp.types import Tool as McpTool


def test_trusted_admin_connection_declares_read_only_and_tushare_protocol():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "provider": "tushare", "server_name": "renamed-provider",
        "server_url": "https://provider.example/mcp", "config_revision": "v1",
    }))) as client:
        config = AdminMarketDataConfigClient(base_url="http://admin", client=client).get_active_mcp_config()
    assert config.effect_type == "read_only"
    assert config.error_protocol == "tushare"
    assert config.default_risk_level == "medium"


def test_discovery_preserves_host_effect_policy_in_frozen_spec(monkeypatch):
    class Client:
        def __init__(self):
            self.connections = {"renamed-provider": {"transport": "streamable_http", "url": "https://provider.example/mcp"}}
        @asynccontextmanager
        async def session(self, server_name):
            async def list_tools(**kwargs):
                return SimpleNamespace(tools=[McpTool(name="future_query", inputSchema={"type": "object"})], nextCursor=None)
            yield SimpleNamespace(list_tools=list_tools)

    monkeypatch.setattr(McpHttpClient, "_client", lambda *_: Client())
    config = McpServerConfig(
        name="renamed-provider", url="https://provider.example/mcp",
        effect_type="read_only", error_protocol="tushare",
    )
    tool = McpGateway().discover_tools(config)[0]
    assert tool.spec.effect_type == "read_only"
    assert tool.spec.risk_level == "medium"
    generic = config.model_copy(update={"effect_type": "unknown", "error_protocol": "generic"})
    assert McpGateway.execution_binding(config, tool=tool).digest != McpGateway.execution_binding(generic, tool=tool).digest


@pytest.mark.parametrize("field", ["code", "errorcode", "error_code"])
@pytest.mark.parametrize("structured", [False, True])
def test_documented_permission_code_does_not_depend_on_error_words(monkeypatch, field, structured):
    import json

    tool = DynamicMcpHttpTool(
        name="mcp.renamed.query", server_name="renamed", description="查询",
        input_schema={"type": "object"}, langchain_tool=_langchain_tool("query", {}),
        permission_denial_is_pre_execution=True, error_protocol="tushare", effect_type="read_only",
    )
    payload = {field: 2002, "msg": "任意语言的服务端说明", "data": None}

    async def invoke(_):
        return ToolMessage(
            content="服务端错误" if structured else json.dumps(payload),
            artifact={"structured_content": payload} if structured else None,
            status="error", tool_call_id="remote",
        )

    monkeypatch.setattr(type(tool._langchain_tool), "ainvoke", lambda _, value: invoke(value))
    result = asyncio.run(tool.run(tool.input_model.model_validate({})))
    assert result.error.code == "provider_permission_denied"
    assert result.error.details["provider_code"] == 2002
    assert result.execution_state == "not_started"


@pytest.mark.parametrize("message", ["没有权限", "频率超限", "完全不同的文案"])
def test_ambiguous_40203_preserves_code_without_guessing_from_words(monkeypatch, message):
    tool = DynamicMcpHttpTool(
        name="mcp.renamed.query", server_name="renamed", description="查询",
        input_schema={"type": "object"}, langchain_tool=_langchain_tool("query", {}),
        permission_denial_is_pre_execution=True, error_protocol="tushare", effect_type="read_only",
    )

    async def invoke(_):
        return ToolMessage(content=[{"type": "text", "text": f"tushare API 错误 [40203]: {message}"}],
                           status="error", tool_call_id="remote")

    monkeypatch.setattr(type(tool._langchain_tool), "ainvoke", lambda _, value: invoke(value))
    result = asyncio.run(tool.run(tool.input_model.model_validate({})))
    assert result.error.code == "provider_request_rejected"
    assert result.error.details["provider_code"] == 40203
    assert result.execution_state == "not_started"


def test_server_name_alone_does_not_enable_tushare_error_policy():
    tool = DynamicMcpHttpTool(
        name="mcp.tushare.query", server_name="tushare", description="未受信连接",
        input_schema={"type": "object"},
        langchain_tool=_langchain_tool("query", {"code": 2002, "msg": "没有权限", "data": None}),
    )
    assert tool.spec.effect_type == "unknown"
    result = asyncio.run(tool.run(tool.input_model.model_validate({})))
    assert result.error.code == "mcp_tool_execution_failed"
    assert result.execution_state == "uncertain"


@pytest.mark.parametrize("payload", [
    {"code": 2002, "msg": "说明"},
    {"errorcode": "2002", "message": "说明"},
])
def test_known_tushare_code_without_data_field_is_still_a_failure(payload):
    tool = DynamicMcpHttpTool(
        name="mcp.renamed.query", server_name="renamed", description="查询",
        input_schema={"type": "object"}, langchain_tool=_langchain_tool("query", payload),
        permission_denial_is_pre_execution=True, error_protocol="tushare", effect_type="read_only",
    )
    result = asyncio.run(tool.run(tool.input_model.model_validate({})))
    assert result.error.code == "provider_permission_denied"
    assert result.execution_state == "not_started"


def test_low_risk_write_still_has_unknown_side_effects():
    from app.schemas.tools import ToolCallResult
    from app.tools.execution_gateway import execution_is_uncertain

    result = ToolCallResult(tool_call_id="write", tool_name="write", status="timeout", trace_id="")
    assert execution_is_uncertain(result, risk_level="low", effect_type="write")
    assert not execution_is_uncertain(result, risk_level="medium", effect_type="read_only")
