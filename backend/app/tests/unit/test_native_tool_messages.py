import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel

from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.state import create_initial_state, merge_tool_call_records
from app.schemas.approvals import ApprovedToolCall
from app.schemas.messages import Message
from app.schemas.tools import ToolCallResult, ToolSpec
from app.tools.base import Tool
from app.tools.native_calls import create_tool_call_message, create_tool_result_message
from app.tools.native_calls import ToolCallCountError, UnexpectedToolCallError
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.registry import ToolRegistry


class BoundModel:
    def __init__(self, client):
        self.client = client

    def invoke(self, messages):
        self.client.messages = messages
        assert isinstance(messages[-1], HumanMessage)
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call-1",
                    "name": "mcp_tushare_stock_daily",
                    "args": {"symbol": "002583.SZ"},
                    "type": "tool_call",
                }
            ],
        )


class ToolCallingClient:
    def __init__(self) -> None:
        self.tools = None
        self.tool_choice = None
        self.bind_calls = 0
        self.messages = []

    def bind_tools(self, tools, *, tool_choice):
        self.bind_calls += 1
        self.tools = tools
        self.tool_choice = tool_choice
        return BoundModel(self)


class EmptyBoundModel:
    def invoke(self, messages):
        return AIMessage(content="未调用工具")


class EmptyToolCallingClient(ToolCallingClient):
    def bind_tools(self, tools, *, tool_choice):
        return EmptyBoundModel()


class WrongToolBoundModel:
    def invoke(self, messages):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call-1",
                    "name": "another_tool",
                    "args": {},
                    "type": "tool_call",
                }
            ],
        )


class WrongToolCallingClient(ToolCallingClient):
    def bind_tools(self, tools, *, tool_choice):
        return WrongToolBoundModel()


def _spec() -> ToolSpec:
    return ToolSpec(
        name="mcp.tushare.stock.daily",
        version="1.0.0",
        description="查询 A 股日线行情",
        input_schema={
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    )


def test_native_tool_call_uses_bound_schema_and_restores_registry_name():
    client = ToolCallingClient()

    message = create_tool_call_message(
        llm_client=client,
        messages=[HumanMessage(content="查询海能达行情")],
        tool_spec=_spec(),
    )

    assert client.tool_choice == "mcp_tushare_stock_daily"
    assert client.tools[0]["function"]["name"] == "mcp_tushare_stock_daily"
    assert "api_key" not in str(client.tools)
    assert message.tool_calls == [
        {
            "id": "call-1",
            "name": "mcp.tushare.stock.daily",
            "args": {"symbol": "002583.SZ"},
            "type": "tool_call",
        }
    ]


def test_native_tool_call_distinguishes_missing_call_from_wrong_tool():
    with pytest.raises(ToolCallCountError):
        create_tool_call_message(
            llm_client=EmptyToolCallingClient(),
            messages=[HumanMessage(content="查询海能达行情")],
            tool_spec=_spec(),
        )

    with pytest.raises(UnexpectedToolCallError):
        create_tool_call_message(
            llm_client=WrongToolCallingClient(),
            messages=[HumanMessage(content="查询海能达行情")],
            tool_spec=_spec(),
        )


def test_tool_result_message_matches_call_id_and_keeps_raw_payload_in_artifact():
    message = create_tool_result_message(
        tool_call_id="call-1",
        tool_name="mcp.tushare.stock.daily",
        content='{"status":"success","price":8.12}',
        artifact={"provider_payload": {"close": 8.12}, "token": "must-not-leak"},
    )

    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == "call-1"
    assert message.name == "mcp.tushare.stock.daily"
    assert "provider_payload" not in message.content
    assert message.artifact == {
        "provider_payload": {"close": 8.12},
        "token": "***",
    }


def test_initial_state_converts_persisted_messages_to_langchain_messages():
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="当前问题",
        user_message_id="current-user",
        history=[
            Message(
                message_id="history-user",
                role="user",
                content="上一问",
                metadata={"request_id": "previous"},
            ),
            Message(
                message_id="history-assistant",
                role="assistant",
                content="上一答",
            ),
        ],
    )

    assert [message.type for message in state["messages"]] == [
        "human",
        "ai",
        "human",
    ]
    assert [message.id for message in state["messages"]] == [
        "history-user",
        "history-assistant",
        "current-user",
    ]
    assert state["messages"][0].additional_kwargs["metadata"] == {
        "request_id": "previous"
    }


class DailyInput(BaseModel):
    symbol: str


class DailyTool(Tool[DailyInput]):
    name = "mcp.tushare.stock.daily"
    version = "1.0.0"
    description = "查询 A 股日线行情"
    input_model = DailyInput

    async def run(self, input_data: DailyInput) -> ToolCallResult:
        return ToolCallResult(
            tool_call_id="provider-call",
            tool_name=self.name,
            status="success",
            result={"symbol": input_data.symbol, "close": 8.12},
            trace_id="provider-trace",
        )


def test_tool_worker_appends_native_messages_and_task_result():
    registry = ToolRegistry()
    registry.register(DailyTool())
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="查询海能达行情",
        permission_scopes=[],
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {
        "id": "market",
        "task_goal": "查询海能达行情",
        "depends_on": ["resolve_symbol"],
        "skill_name": None,
        "tool_name": DailyTool.name,
    }
    state["task_results"] = [
        {
            "task_id": "resolve_symbol",
            "status": "success",
            "content": '{"symbol":"002583.SZ"}',
            "error_code": None,
        }
    ]
    state["messages"] = add_messages(
        state["messages"],
        [
            AIMessage(
                id="internal-call",
                content="",
                tool_calls=[
                    {
                        "id": "old-call",
                        "name": "web_search",
                        "args": {"query": "海能达"},
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                id="internal-result",
                content='{"symbol":"002583.SZ"}',
                tool_call_id="old-call",
                name="web_search",
            ),
        ],
    )

    llm = ToolCallingClient()
    update = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=llm,
    )(state)

    instruction = json.loads(llm.messages[-1].content)
    assert instruction["task_goal"] == "查询海能达行情"
    assert instruction["dependencies"][0]["task_id"] == "resolve_symbol"
    assert not any(isinstance(message, ToolMessage) for message in llm.messages)
    assert not any(
        isinstance(message, AIMessage) and message.tool_calls
        for message in llm.messages
    )
    assert [message.type for message in update["messages"][-2:]] == ["ai", "tool"]
    ai_message, tool_message = update["messages"][-2:]
    assert tool_message.tool_call_id == ai_message.tool_calls[0]["id"]
    assert update["task_results"] == [
        {
            "task_id": "market",
            "status": "success",
            "content": '{"close":8.12,"symbol":"002583.SZ"}',
            "error_code": None,
        }
    ]
    assert update["tool_calls"][0].request.arguments == {"symbol": "002583.SZ"}


def test_high_risk_tool_reuses_staged_call_after_approval():
    registry = ToolRegistry()
    tool = DailyTool()
    tool.risk_level = "high"
    registry.register(tool)
    registry = registry.snapshot()
    llm = ToolCallingClient()
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="查询海能达行情",
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {
        "id": "market",
        "task_goal": "查询海能达行情",
        "depends_on": [],
        "skill_name": None,
        "tool_name": DailyTool.name,
    }
    worker = build_tool_agent_node(registry_factory=lambda: registry, llm_client=llm)

    staged = worker(state)
    request = staged["tool_calls"][0].request
    state["messages"] = add_messages(state["messages"], staged["messages"])
    state["tool_calls"] = merge_tool_call_records(
        state["tool_calls"], staged["tool_calls"]
    )
    state["pending_tool_approval"] = staged["pending_tool_approval"]
    state["approved_tool_calls"].append(
        ApprovedToolCall(
            call_id=request.call_id or "",
            arguments_hash=tool_call_fingerprint(request),
        )
    )
    state["pending_tool_approval"] = None

    completed = worker(state)

    assert llm.bind_calls == 1
    assert [message.type for message in completed["messages"]] == ["tool"]
    assert completed["tool_calls"][0].result.status == "success"
