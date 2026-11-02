import json

import pytest
from app.graph.nodes.tool_agent import (
    _generate_tool_selection,
    _task_result,
    _tool_call_messages,
    _tool_message_content,
    build_tool_agent_node,
)
from app.graph.planning.contracts import SubTaskModel
from app.graph.state import create_initial_state, merge_tool_call_records
from app.schemas.approvals import ApprovedToolCall
from app.schemas.messages import Message
from app.schemas.tools import ToolCallResult, ToolError, ToolSpec
from app.schemas.user_input import UserInputResponse
from app.tools.base import Tool
from app.tools.candidate_resolver import ToolCandidateResolution, ToolCandidateResolver
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.native_calls import (
    ToolCallCountError,
    UnexpectedToolCallError,
    create_tool_call_message,
    create_tool_result_message,
)
from app.tools.registry import ToolRegistry
from app.tools.search import ToolSearchHit, ToolSearchResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel


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


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([], "[]"),
        (0, "0"),
        (False, "false"),
        ("", '""'),
        (None, "null"),
    ],
)
def test_tool_message_content_preserves_json_falsey_values(payload, expected):
    result = ToolCallResult(
        tool_call_id="tool-1",
        tool_name="mcp.tushare.stock_company",
        status="success",
        result=payload,
        trace_id="trace-1",
    )

    assert _tool_message_content(result) == expected


def test_native_tool_call_uses_bound_schema_and_restores_registry_name():
    client = ToolCallingClient()

    message = create_tool_call_message(
        llm_client=client,
        messages=[HumanMessage(content="查询海能达行情")],
        tool_spec=_spec(),
    )

    assert client.tool_choice == "required"
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


@pytest.mark.parametrize("choice", ["auto", "required"])
def test_first_tool_request_has_single_call_contract_without_mutating_messages(choice):
    client = ToolCallingClient()
    original = [SystemMessage(content="业务约束"), HumanMessage(content="查询行情")]
    before = list(original)

    create_tool_call_message(
        llm_client=client, messages=original, tool_spec=_spec(), tool_choice=choice,
    )

    contract = client.messages[0]
    assert isinstance(contract, SystemMessage)
    assert "严禁一次返回多个" in contract.content
    assert "同一工具" in contract.content
    assert ("允许不调用" if choice == "auto" else "必须恰好调用一个") in contract.content
    assert client.messages[1:] == before
    assert original == before


@pytest.mark.parametrize("fixed_skill", [False, True])
def test_tool_request_includes_current_datetime_and_preserves_historical_period(
    monkeypatch, fixed_skill,
):
    from datetime import datetime
    from types import SimpleNamespace

    from app.graph.nodes import tool_agent as node_module

    if fixed_skill:
        monkeypatch.setattr(node_module, "active_step", lambda _state: (
            SimpleNamespace(summary="历史估值查询"),
                SimpleNamespace(source="查询2025年估值", required_fields=[], argument_retry_count=0),
        ))

    state = create_initial_state(
        session_id="date", tenant_id="t1", user_id="u1",
        user_message="查询2025年估值", history=[],
    )
    messages = _tool_call_messages(state, SubTaskModel(id="facts", task_goal="查询2025年估值"))
    instruction = json.loads(messages[-2 if fixed_skill else -1].content)

    timestamp = datetime.fromisoformat(instruction["current_datetime"])
    assert timestamp.utcoffset().total_seconds() == 8 * 3600
    assert instruction["timezone"] == "Asia/Shanghai"
    assert instruction["task_goal"] == "查询2025年估值"
    assert "历史" in messages[0].content
    assert "current_datetime" in messages[0].content


def test_native_tool_call_distinguishes_missing_call_from_wrong_tool():
    with pytest.raises(ToolCallCountError):
        create_tool_call_message(
            llm_client=EmptyToolCallingClient(),
            messages=[HumanMessage(content="查询海能达行情")],
            tool_spec=_spec(),
        )


def test_tool_call_messages_keep_context_compaction_once_in_bounded_materials():
    summary_payload = {
        "goal": "长期目标",
        "constraints": [],
        "decisions": [],
        "open_items": [],
        "facts": [],
        "evidence_refs": [],
        "user_preferences": [],
        "superseded": [],
    }
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="继续",
        history=[
            Message(
                message_id="cmp-1",
                role="assistant",
                content=json.dumps(summary_payload, ensure_ascii=False),
                metadata={"context_compaction": True},
            )
        ],
    )
    task = SubTaskModel(id="facts", task_goal="核实事实")

    messages = _tool_call_messages(state, task)

    payload = json.loads(messages[1].content)
    summaries = [item for item in payload["user_materials"] if item["kind"] == "context_summary"]
    assert len(summaries) == 1
    assert json.loads(summaries[0]["text"]) == summary_payload
    assert sum(str(message.content).count("长期目标") for message in messages) == 1

    with pytest.raises(UnexpectedToolCallError):
        create_tool_call_message(
            llm_client=WrongToolCallingClient(),
            messages=[HumanMessage(content="查询海能达行情")],
            tool_spec=_spec(),
        )


def test_auto_selection_repairs_multiple_model_tool_calls_once():
    class MultipleThenSingle:
        def __init__(self):
            self.messages = []

        def bind_tools(self, tools, *, tool_choice):
            assert tool_choice == "auto"
            client = self

            class Bound:
                def invoke(self, messages):
                    client.messages.append(messages)
                    count = 2 if len(client.messages) == 1 else 1
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": f"call-{index}",
                                "name": "mcp_tushare_stock_daily",
                                "args": {"symbol": "002583.SZ"},
                                "type": "tool_call",
                            }
                            for index in range(count)
                        ],
                    )

            return Bound()

    client = MultipleThenSingle()
    result = _generate_tool_selection(
        llm_client=client,
        messages=[HumanMessage(content="核实行情")],
        task=SubTaskModel(id="facts", task_goal="核实行情"),
        resolution=ToolCandidateResolution(
            candidates=(_spec(),), hits=(), tool_choice="auto", fallback_reason=None
        ),
    )
    assert len(client.messages) == 2
    assert "最多调用一个" in str(client.messages[1][-1].content)
    assert result.selected_tool.name == _spec().name


def test_native_tool_call_selects_one_of_ranked_candidates():
    client = ToolCallingClient()
    basic = ToolSpec(
        name="mcp.tushare.stock.daily_basic",
        version="1.0.0",
        description="查询 A 股估值指标",
        input_schema={"type": "object", "properties": {}},
    )

    selection = create_tool_call_message(
        llm_client=client,
        messages=[HumanMessage(content="查询海能达日线行情")],
        tool_specs=[_spec(), basic],
        tool_choice="required",
    )

    assert [item["function"]["name"] for item in client.tools] == [
        "mcp_tushare_stock_daily",
        "mcp_tushare_stock_daily_basic",
    ]
    assert selection.selected_tool is not None
    assert selection.selected_tool.name == "mcp.tushare.stock.daily"
    assert selection.message.tool_calls[0]["name"] == "mcp.tushare.stock.daily"


def test_auto_tool_choice_accepts_plain_analysis_content():
    selection = create_tool_call_message(
        llm_client=EmptyToolCallingClient(),
        messages=[HumanMessage(content="综合已有数据")],
        tool_specs=[_spec()],
        tool_choice="auto",
    )

    assert selection.selected_tool is None
    assert selection.message.content == "未调用工具"


def test_tool_argument_dates_follow_declared_format_before_request_binding():
    from app.graph.nodes.tool_agent import _repair_arguments_once

    state=create_initial_state(session_id="s",tenant_id="t",user_id="u",user_message="查询历史")
    spec=ToolSpec(name="mcp.any.history",version="1",description="历史数据",input_schema={
        "type":"object","properties":{
            "trade_date":{"type":"string","description":"交易日期（格式：YYYYMMDD，下同）"},
            "start_date":{"type":"string","description":"开始日期"},
            "query":{"type":"string"},
        }})
    from app.tools.native_calls import NativeToolSelection
    message=AIMessage(content="",tool_calls=[{"id":"untrusted","name":spec.name,"type":"tool_call",
        "args":{"trade_date":"2025-09-01","start_date":"2025-08-01","query":"资料发布于2025-09-01"}}])
    repaired=_repair_arguments_once(llm_client=None,state=state,task=SubTaskModel(id="history",task_goal="历史查询"),
        selection=NativeToolSelection(message=message,selected_tool=spec))
    assert repaired.message.tool_calls[0]["args"]=={
        "trade_date":"20250901","start_date":"20250801","query":"资料发布于2025-09-01"}
    assert message.tool_calls[0]["args"]["trade_date"]=="2025-09-01"


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
    assert state["messages"][0].additional_kwargs["metadata"] == {"request_id": "previous"}


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


class PermissionDeniedDailyTool(DailyTool):
    async def run(self, input_data: DailyInput) -> ToolCallResult:
        return ToolCallResult(
            tool_call_id="provider-call",
            tool_name=self.name,
            status="failed",
            error=ToolError(
                code="provider_permission_denied",
                message="市场数据源权限不足。",
                retryable=False,
            ),
            trace_id="provider-trace",
        )


class ResolveCompanyTool(DailyTool):
    name = "mcp.tushare.stock.resolve_company"
    description = "解析 A 股公司名称"

    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0

    async def run(self, input_data: DailyInput) -> ToolCallResult:
        self.calls += 1
        return ToolCallResult(
            tool_call_id="provider-call",
            tool_name=self.name,
            status="failed",
            error=ToolError(
                code="required_data_missing",
                message="公司名称无法唯一解析。",
                details={"candidates": self.candidates},
            ),
            trace_id="provider-trace",
        )


class ResolveCompanyClient(ToolCallingClient):
    def bind_tools(self, tools, *, tool_choice):
        self.bind_calls += 1

        class Bound:
            def invoke(_self, messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "model-call",
                            "name": "mcp_tushare_stock_resolve_company",
                            "args": {"symbol": "长鑫存储"},
                            "type": "tool_call",
                        }
                    ],
                )

        return Bound()


class WebSearchInput(BaseModel):
    query: str


class WebSearchTool(Tool[WebSearchInput]):
    name = "web_search"
    version = "1.0.0"
    description = "查询公开网页"
    input_model = WebSearchInput

    async def run(self, input_data: WebSearchInput) -> ToolCallResult:
        return ToolCallResult(
            tool_call_id="provider-search",
            tool_name=self.name,
            status="success",
            result={"query": input_data.query},
            trace_id="trace-search",
        )


class WebOnlySearchService:
    catalog_digest = "digest"

    def search(self, request, *, context):
        return ToolSearchResponse(
            catalog_digest=self.catalog_digest,
            mode="bm25",
            hits=[
                ToolSearchHit(
                    tool_name="web_search",
                    tool_version="1.0.0",
                    description="查询公开网页",
                    rank=1,
                    sources=["bm25"],
                )
            ],
        )

    def resolve_specs(self, names):
        assert names == ["web_search"]
        return [WebSearchTool().spec]


class NoToolCallClient(ToolCallingClient):
    def bind_tools(self, tools, *, tool_choice):
        self.bind_calls += 1

        class Bound:
            def invoke(_self, messages):
                return AIMessage(content="缺少可确定的证券代码，无法生成参数。")

        return Bound()


def test_tool_worker_accepts_model_decision_not_to_call():
    registry = ToolRegistry()
    registry.register(DailyTool())
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="比较银行板块代表性标的",
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {
        "id": "market",
        "task_goal": "获取银行板块代表性标的的行情快照",
        "depends_on": [],
        "skill_name": None,
        "tool_name": DailyTool.name,
    }

    update = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=NoToolCallClient(),
    )(state)

    assert update["task_results"][0]["status"] == "success"
    assert not update.get("tool_calls")
    from app.graph.planning.context import user_materials

    state["messages"]=add_messages(state["messages"],update["messages"])
    assert update["messages"][0].additional_kwargs["task_role"]=="tool_analysis"
    assert "缺少可确定的证券代码" not in json.dumps(user_materials(state),ensure_ascii=False)


def test_tool_worker_does_not_retry_a_valid_no_call_decision():
    registry = ToolRegistry()
    registry.register(DailyTool())
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="session-local-retry",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="比较银行板块代表性标的",
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {
        "id": "market",
        "task_goal": "获取银行板块代表性标的的行情快照",
        "depends_on": [],
        "skill_name": None,
        "tool_name": DailyTool.name,
    }
    llm = NoToolCallClient()

    update = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=llm,
    )(state)

    assert llm.bind_calls == 1
    assert not update.get("tool_calls")


def test_web_search_legacy_path_respects_model_no_call():
    registry = ToolRegistry()
    registry.register(WebSearchTool())
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="session-web-search",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="你觉得 A 股要变盘了吗",
        permission_scopes=[],
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {
        "id": "market_signals",
        "task_goal": "检索最近十个交易日 A 股主要指数与成交额变化",
        "depends_on": [],
        "skill_name": None,
        "tool_name": WebSearchTool.name,
    }
    llm = NoToolCallClient()

    update = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=llm,
    )(state)

    assert llm.bind_calls == 1
    assert update["task_results"][0]["status"] == "success"
    assert not update.get("tool_calls")


def test_web_only_candidates_respect_model_no_call():
    registry = ToolRegistry()
    registry.register(WebSearchTool())
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="session-web-fallback",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析一下今日行情",
        permission_scopes=["web:search"],
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {
        "id": "fetch_market_data",
        "task_goal": "获取今日A股、港股、美股、汇率、商品和国债收益率",
        "depends_on": [],
        "skill_name": None,
    }
    llm = NoToolCallClient()
    resolver = ToolCandidateResolver(search_service=WebOnlySearchService())

    update = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=llm,
        candidate_resolver=resolver,
    )(state)

    assert llm.bind_calls == 1
    assert update["task_results"][0]["status"] == "success"
    assert not update.get("tool_calls")


def test_market_data_configuration_failure_stops_unrecoverable_replanning():
    task = SubTaskModel(
        id="market",
        task_goal="获取银之杰行情快照",
    )
    result = ToolCallResult(
        tool_call_id="tool-market",
        tool_name="mcp.tushare.stock.market_snapshot",
        status="failed",
        error=ToolError(
            code="market_data_configuration_invalid",
            message="市场数据源配置无效。",
            retryable=False,
        ),
        trace_id="trace-market",
    )

    task_result = _task_result(task, result, "市场数据源配置无效。")

    assert task_result.status == "terminal_failure"


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
        isinstance(message, AIMessage) and message.tool_calls for message in llm.messages
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
            "tool_name": DailyTool.name,
        }
    ]
    assert update["tool_calls"][0].request.arguments == {"symbol": "002583.SZ"}


def test_tool_worker_treats_provider_permission_denied_as_replan_observation():
    registry = ToolRegistry()
    registry.register(PermissionDeniedDailyTool())
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="为什么今天 A 股跌得这么多？",
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {
        "id": "market",
        "task_goal": "获取当日市场数据",
        "depends_on": [],
        "skill_name": None,
        "tool_name": PermissionDeniedDailyTool.name,
    }

    update = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=ToolCallingClient(),
    )(state)

    assert update["task_results"][0]["status"] == "business_failure"
    assert update["task_results"][0]["error_code"] == "provider_permission_denied"
    assert update["task_results"][0]["tool_name"] == PermissionDeniedDailyTool.name


def test_company_resolution_candidates_create_generic_user_input_request():
    tool = ResolveCompanyTool(
        [
            {"symbol": "688825.SH", "name": "C长鑫", "score": 1.0},
            {"symbol": "603986.SH", "name": "兆易创新", "score": 0.82},
        ]
    )
    registry = ToolRegistry()
    registry.register(tool)
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="session-selection",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="长鑫存储是哪只股票？",
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {
        "id": "resolve_company",
        "task_goal": "解析长鑫存储对应的证券代码",
        "tool_name": tool.name,
    }

    update = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=ResolveCompanyClient(),
    )(state)

    request = update["pending_user_input"]
    assert request.input_type == "selection"
    assert [(item.value, item.label) for item in request.options] == [
        ("688825.SH", "C长鑫"),
        ("603986.SH", "兆易创新"),
    ]
    assert request.source.node == "tool_agent"
    assert request.source.kind == "entity_disambiguation"
    assert request.source.task_id == "resolve_company"
    assert request.source.call_id == update["tool_calls"][0].request.call_id
    assert "task_results" not in update


def test_company_resolution_single_candidate_keeps_business_failure():
    tool = ResolveCompanyTool([{"symbol": "688825.SH", "name": "C长鑫"}])
    registry = ToolRegistry()
    registry.register(tool)
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="session-single",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="长鑫存储是哪只股票？",
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {
        "id": "resolve_company",
        "task_goal": "解析公司",
        "tool_name": tool.name,
    }

    update = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=ResolveCompanyClient(),
    )(state)

    assert update["task_results"][0]["status"] == "business_failure"
    assert update["task_results"][0]["error_code"] == "required_data_missing"
    assert "pending_user_input" not in update


def test_company_resolution_resume_succeeds_without_reexecuting_tool():
    tool = ResolveCompanyTool(
        [
            {"symbol": "688825.SH", "name": "C长鑫"},
            {"symbol": "603986.SH", "name": "兆易创新"},
        ]
    )
    registry = ToolRegistry()
    registry.register(tool)
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="session-resume",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="长鑫存储是哪只股票？",
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {
        "id": "resolve_company",
        "task_goal": "解析公司",
        "tool_name": tool.name,
    }
    worker = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=ResolveCompanyClient(),
    )
    interrupted = worker(state)
    state["tool_calls"] = merge_tool_call_records(
        state["tool_calls"], interrupted["tool_calls"]
    )
    request = interrupted["pending_user_input"]
    state["pending_user_input"] = None
    state["user_input_response"] = UserInputResponse(
        interaction_id=request.interaction_id,
        input_type="selection",
        value="688825.SH",
        source=request.source,
    )

    completed = worker(state)

    assert tool.calls == 1
    assert completed["user_input_response"] is None
    assert completed["task_results"] == [
        {
            "task_id": "resolve_company",
            "status": "success",
            "content": '{"resolution":"user_selection","symbol":"688825.SH"}',
            "error_code": None,
            "tool_name": tool.name,
        }
    ]
    assert completed["tool_calls"][0].result.status == "success"


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
    state["tool_calls"] = merge_tool_call_records(state["tool_calls"], staged["tool_calls"])
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


def test_web_search_executes_model_query_not_task_goal():
    class Client(ToolCallingClient):
        def bind_tools(self, tools, *, tool_choice):
            assert tool_choice == "auto"

            class Bound:
                def invoke(self, messages):
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "model",
                                "name": "web_search",
                                "args": {"query": "公司公告 2026 年度 原文"},
                                "type": "tool_call",
                            }
                        ],
                    )

            return Bound()

    registry = ToolRegistry()
    registry.register(WebSearchTool())
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="s",
        tenant_id="t",
        user_id="u",
        user_message="分析原因",
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {"id": "facts", "task_goal": "核实原因"}
    result = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=Client(),
        candidate_resolver=ToolCandidateResolver(search_service=WebOnlySearchService()),
    )(state)
    assert result["tool_calls"][0].request.arguments == {"query": "公司公告 2026 年度 原文"}


def test_timeout_is_recorded_without_reinvoking_model():
    class TimeoutTool(DailyTool):
        async def run(self, input_data):
            return ToolCallResult(
                tool_call_id="remote",
                tool_name=self.name,
                status="timeout",
                trace_id="remote",
                error=ToolError(code="mcp_tool_timeout", message="超时", retryable=True),
            )

    registry = ToolRegistry()
    registry.register(TimeoutTool())
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="s",
        tenant_id="t",
        user_id="u",
        user_message="查行情",
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {"id": "facts", "task_goal": "查行情", "tool_name": DailyTool.name}
    client = ToolCallingClient()
    result = build_tool_agent_node(registry_factory=lambda: registry, llm_client=client)(state)
    assert client.bind_calls == 1
    assert result["tool_calls"][0].result.status == "timeout"


def test_exhausted_search_budget_keeps_call_and_denial_record():
    from app.tools.call_budget import SearchCallBudget

    class Client:
        def bind_tools(self, tools, *, tool_choice):
            class Bound:
                def invoke(self, messages):
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "model",
                                "name": "web_search",
                                "args": {"query": "公告"},
                                "type": "tool_call",
                            }
                        ],
                    )

            return Bound()

    registry = ToolRegistry()
    registry.register(WebSearchTool())
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="s",
        tenant_id="t",
        user_id="u",
        user_message="查询公告",
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {"id": "facts", "task_goal": "查询公告"}
    result = build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=Client(),
        candidate_resolver=ToolCandidateResolver(search_service=WebOnlySearchService()),
        search_budget=SearchCallBudget(limit=0),
    )(state)
    assert result["tool_calls"][0].result.error.code == "research_budget_exhausted"
    assert result["tool_calls"][0].result.status == "denied"
    assert result["messages"][-1].tool_call_id == result["messages"][-2].tool_calls[0]["id"]
