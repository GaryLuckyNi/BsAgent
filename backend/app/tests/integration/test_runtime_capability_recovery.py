"""真实 Gateway、独立回执和 Graph 的受控批次恢复，不访问外部服务。"""

import json

import pytest
from app.conversations.execution import execution_control
from app.core import config as app_config
from app.graph.nodes import tool_agent
from app.graph.nodes.user_input_gate import user_input_gate_node
from app.graph.recovery.batching import capture_dispatch
from app.graph.recovery.executor import accept_confirmation, build_recovery_node
from app.graph.runtime import _dispatch_after_scheduler, build_task_scheduler_node
from app.graph.state import AgentState, create_initial_state
from app.schemas.recovery import RECOVERY_VERSION
from app.storage.models import AgentMessage, AgentRun
from app.tests.unit.test_durable_turns import database  # noqa: F401
from app.tests.unit.test_langfuse_scheduler import telemetry as telemetry_runtime  # noqa: F401
from app.tests.unit.test_permission_fallback import setup_worker
from app.tests.unit.test_recovery_durability import control as durable_control  # noqa: F401
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command
from sqlalchemy import select


@pytest.fixture
def control(request):
    return request.getfixturevalue("durable_control")


@pytest.fixture
def telemetry(request):
    return request.getfixturevalue("telemetry_runtime")


def batch_graph(control, monkeypatch, *, recovery_hook=None, policy=None, planner_node=None,
                search_tool=None, healthy_uses_gateway=False):
    monkeypatch.setattr(app_config, "settings", app_config.settings.model_copy(
        update={"permission_recovery_source_policy": "public_supplement_only"}
    ))
    worker, _, data, search, client, registry = (
        setup_worker() if search_tool is None else setup_worker(search_tool=search_tool)
    )
    # 默认使用生产来源解析；只在非法/缺失快照用例注入错误，不再注入 allowed 替身。
    if policy is not None:
        original_context = tool_agent.selection_context

        def context(*args, **kwargs):
            value = original_context(*args, **kwargs)
            value["source_policy"] = policy
            return value

        monkeypatch.setattr(tool_agent, "selection_context", context)
    with control.session_factory() as db:
        run = db.get(AgentRun, control.run_id)
        assistant = db.scalar(
            select(AgentMessage).where(
                AgentMessage.run_id == run.run_id, AgentMessage.role == "assistant"
            )
        )
        state = create_initial_state(
            session_id=run.session_id,
            session_key=run.session_key,
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            user_message="查询利润表和现金流",
            request_id="r",
            assistant_message_id=assistant.message_id,
            trace_id=run.trace_id,
            tool_catalog_digest=registry.catalog_digest,
            permission_scopes=["web:search"],
        )
    state["recovery_policy_version"] = RECOVERY_VERSION
    state["intent"] = {"source_constraint": "unrestricted"}
    state["plan"] = {
        "tasks": [
            {"id": "income", "task_goal": "查询利润表公开披露", "completion_criteria": ["保留报告期间"],
             "source_requirement": "public_information"},
            {"id": "cashflow", "task_goal": "查询现金流公开披露", "completion_criteria": ["保留报告期间"],
             "source_requirement": "public_information"},
            {"id": "healthy", "task_goal": "查询独立公开事实", "execution_type": "tool"},
        ]
    }
    builder = StateGraph(AgentState)
    builder.add_node("task_scheduler", build_task_scheduler_node(registry))

    def tool_worker(state):
        if state["active_task"]["id"] == "healthy" and not healthy_uses_gateway:
            return {
                "task_results": [
                    {"task_id": "healthy", "status": "success", "content": "健康任务结果"}
                ]
            }
        return worker(state)

    builder.add_node("tool_agent", capture_dispatch(tool_worker))
    builder.add_node(
        "user_input_gate",
        lambda state: user_input_gate_node(
            state, accept_recovery=lambda *args: accept_confirmation(*args, registry=registry)
        ),
    )
    recovery = build_recovery_node(registry)

    def recover(state):
        update = recovery(state)
        if recovery_hook:
            recovery_hook(update)
        return update

    builder.add_node("permission_recovery", recover)
    builder.add_node("planner_agent", planner_node or (lambda state: {}))
    builder.add_node("synthesizer", lambda state: {})
    builder.set_entry_point("task_scheduler")
    builder.add_conditional_edges("task_scheduler", _dispatch_after_scheduler)
    for node in ("tool_agent", "user_input_gate", "permission_recovery"):
        builder.add_edge(node, "task_scheduler")
    builder.add_edge("planner_agent", END)
    builder.add_edge("synthesizer", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    return graph, state, data, search, client, registry


def test_legacy_batch_tasks_resume_without_rewriting_sealed_snapshots(control, monkeypatch):
    original = tool_agent.selection_context

    def legacy_context(*args, **kwargs):
        context = original(*args, **kwargs)
        context["task"].pop("tool_candidate_refs")
        return context

    monkeypatch.setattr(tool_agent, "selection_context", legacy_context)
    graph, state, data, search, client, _ = batch_graph(control, monkeypatch)
    config = {"configurable": {"thread_id": "legacy-candidate-field"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        payload = paused["__interrupt__"][0].value
        contexts = paused["tool_selection_contexts"]
        resumed = graph.invoke(Command(resume={
            "interaction_id": payload["interaction_id"], "input_type": "selection", "value": "web_search",
            "presentation_version": payload["recovery_batch"]["version"],
            "batch_digest": payload["recovery_batch"]["digest"],
        }), config)
    assert search.calls == 2 and data.calls == client.calls == 2
    assert "__interrupt__" not in resumed
    for call_id, context in contexts.items():
        assert resumed["tool_selection_contexts"][call_id]["task"] == context["task"]
        assert "tool_candidate_refs" not in context["task"]


TRACE_RECOVERY_TASK = {
    "id": "get_price_and_target",
    "task_goal": "获取寒武纪（688256.SH）最新收盘价或最新成交价，以及最近一份覆盖该股的卖方研报所给出的目标价（含研报日期、机构名称）；若最近一份研报未给出目标价，则向前追溯至最近一份明确给出目标价的研报并说明。",
    "completion_criteria": [
        "返回寒武纪最新股价，并注明价格日期",
        "返回最近一份研报的目标价、研报日期与机构名称；若最近研报无目标价，需说明并给出最近一份含目标价的研报信息",
        "保留未披露或空值情况，不自行补全目标价",
    ], "source_requirement": "public_information",
}


class RecordingSearchClient:
    def __init__(self):
        self.calls = []

    async def search(self, **kwargs):
        from app.search.schemas import EvidenceCoverage, WebSearchResponse, WebSearchResult

        self.calls.append(kwargs)
        return WebSearchResponse(query=kwargs["query"], results=[WebSearchResult(
            title="公开资料", url="https://example.com/report", snippet="有界资料摘要",
        )], coverage=EvidenceCoverage(status="unknown"))


@pytest.mark.parametrize("choice", ["web_search", "use_existing"])
def test_trace_recovery_uses_valid_goal_query_and_keeps_full_context(control, monkeypatch, choice):
    import asyncio

    from app.schemas.recovery import RecoveryBatch
    from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
    from app.tools.web_search import WebSearchTool

    client = RecordingSearchClient()
    graph, state, data, _, _, registry = batch_graph(
        control, monkeypatch, search_tool=WebSearchTool(client=client),
    )
    state["plan"] = {"tasks": [TRACE_RECOVERY_TASK]}
    config = {"configurable": {"thread_id": "trace-recovery-" + choice}}
    assert len(TRACE_RECOVERY_TASK["task_goal"]) == 101
    assert len("\n".join([TRACE_RECOVERY_TASK["task_goal"],
                          *TRACE_RECOVERY_TASK["completion_criteria"]])) == 192
    with execution_control(control):
        paused = graph.invoke(state, config)
        assert client.calls == [] and data.calls == 1
        payload = paused["__interrupt__"][0].value
        batch = RecoveryBatch.model_validate(next(iter(paused["recovery_batches"].values())))
        assert batch.items[0].task_goal == TRACE_RECOVERY_TASK["task_goal"]
        assert list(batch.items[0].completion_criteria) == TRACE_RECOVERY_TASK["completion_criteria"]
        resumed = graph.invoke(Command(resume={
            "interaction_id": payload["interaction_id"], "input_type": "selection", "value": choice,
            "presentation_version": payload["recovery_batch"]["version"],
            "batch_digest": payload["recovery_batch"]["digest"],
        }), config)
        assert "__interrupt__" not in resumed and data.calls == 1
        assert len(client.calls) == (1 if choice == "web_search" else 0)
        if choice == "web_search":
            sent = client.calls[0]
            assert sent["query"] == TRACE_RECOVERY_TASK["task_goal"]
            assert list(sent["research_context"].completion_criteria) == TRACE_RECOVERY_TASK["completion_criteria"]
            original = next(iter(paused["tool_selection_contexts"].values()))
            assert sent["research_context"].as_of == original["as_of"]
            record = next(row for row in resumed["tool_calls"] if row.request.tool_name == "web_search")
            assert record.result.status == "success"
            reused = asyncio.run(ToolExecutionGateway(registry).execute(record.request, trace_id="t",
                context=ToolExecutionContext(tenant_id=state["tenant_id"], user_id=state["user_id"],
                    session_id=state["session_id"], permission_scopes=("web:search",),
                    allowed_tools=("web_search",), search_budget_limit=3)))
            assert reused.status == "success" and len(client.calls) == 1


@pytest.mark.parametrize("schema_limit", [128, 64])
def test_illegal_recovery_goal_is_rejected_before_confirmation(control, monkeypatch, schema_limit):
    from app.tools.web_search import WebSearchInput, WebSearchTool
    from pydantic import Field

    class ActualInput(WebSearchInput):
        query: str = Field(min_length=1, max_length=schema_limit)

    class ActualTool(WebSearchTool):
        input_model = ActualInput

    client = RecordingSearchClient()
    graph, state, data, _, _, _ = batch_graph(
        control, monkeypatch, search_tool=ActualTool(client=client),
    )
    state["plan"] = {"tasks": [{**TRACE_RECOVERY_TASK, "task_goal": "字" * (schema_limit + 1)}]}
    with execution_control(control):
        stopped = graph.invoke(state, {"configurable": {"thread_id": f"preflight-{schema_limit}"}})
    assert "__interrupt__" not in stopped
    assert not stopped.get("recovery_batches") and not stopped.get("recovery_items")
    assert stopped["task_results"][0]["error_code"] == "recovery_query_too_long"
    assert "长度" in stopped["task_results"][0]["content"]
    assert "替代来源约束" not in stopped["task_results"][0]["content"]
    assert client.calls == [] and data.calls == 1


def test_legacy_deferred_task_compares_semantically_without_rewriting_context(control, monkeypatch):
    from app.graph.planning.contracts import SubTaskModel
    from app.graph.recovery.batching import deferred_sends
    from app.schemas.tools import ToolCallRecord, ToolCallRequest
    from app.tools.fingerprints import tool_call_fingerprint

    _graph, state, _data, _search, _client, _registry = batch_graph(control, monkeypatch)
    task = SubTaskModel.model_validate(state["plan"]["tasks"][0]).model_dump(mode="json")
    task.pop("tool_candidate_refs")
    request = ToolCallRequest(call_id="deferred", tool_name="mcp.tushare.income_vip", arguments={}, purpose="取数")
    state["tool_calls"] = [ToolCallRecord(call_id="deferred", request=request, result=None)]
    state["tool_selection_contexts"] = {"deferred": {
        "request_id": state["request_id"], "task": task, "request_fingerprint": tool_call_fingerprint(request),
    }}
    state["deferred_tool_calls"] = {"deferred": {"task_id": task["id"], "status": "waiting", "dispatch_batch_id": "d"}}
    state["dispatch_batches"] = {"d": {"members": [task["id"]]}}
    with execution_control(control):
        sends = deferred_sends(state)
    assert len(sends) == 1 and sends[0].arg["active_task"] == task
    assert "tool_candidate_refs" not in state["tool_selection_contexts"]["deferred"]["task"]


@pytest.mark.parametrize("choice", ["web_search", "use_existing"])
def test_same_dispatch_refusals_form_one_complete_interrupt_without_model_reselection(
    control, monkeypatch, choice
):
    graph, state, data, search, client, _ = batch_graph(control, monkeypatch)
    config = {"configurable": {"thread_id": "batch"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        assert "__interrupt__" in paused, [row.get("error_code") for row in paused["task_results"]]
        assert len(paused["__interrupt__"]) == 1
        payload = paused["__interrupt__"][0].value
        assert len(payload["recovery_batch"]["items"]) == 2
        assert search.calls == 0 and data.calls == client.calls == 2
        resumed = graph.invoke(
            Command(
                resume={
                    "interaction_id": payload["interaction_id"],
                    "input_type": "selection",
                    "value": choice,
                    "presentation_version": payload["recovery_batch"]["version"],
                    "batch_digest": payload["recovery_batch"]["digest"],
                }
            ),
            config,
        )
    assert "__interrupt__" not in resumed
    assert data.calls == client.calls == 2
    assert search.calls == (2 if choice == "web_search" else 0)
    assert {item["task_id"] for item in resumed["task_results"]} == {
        "income",
        "cashflow",
        "healthy",
    }
    assert (
        next(item for item in resumed["task_results"] if item["task_id"] == "healthy")["content"]
        == "健康任务结果"
    )
    assert "planner_stop_reason" not in resumed


@pytest.mark.parametrize("provider_code", [2002, 40203])
@pytest.mark.parametrize("constraint", ["unrestricted", "original_only", "confirmation_required", "unclear", None])
@pytest.mark.parametrize("requirement", ["public_information", "original_data"])
def test_readonly_rejection_without_multi_source_plan_keeps_original_receipts(
    control, monkeypatch, provider_code, constraint, requirement,
):
    import json

    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_mcp_http_tool import _langchain_tool
    from app.tests.unit.test_permission_fallback import Resolver
    from app.tools.mcp_gateway import McpGateway, McpServerConfig
    from app.tools.mcp_http import DynamicMcpHttpTool
    from app.tools.registry import ToolRegistry
    from langchain_core.messages import AIMessage, ToolMessage

    original=setup_worker
    invoked=[]

    def setup():
        _,state,data,search,client,_=original()
        adapter=DynamicMcpHttpTool(name=data.name,server_name="tushare",
            langchain_tool=_langchain_tool("income_vip",{}),description="年报公开披露",
            input_schema={"type":"object","properties":{"dataset":{"type":"string"}},"required":["dataset"]},
            permission_scope=[],permission_denial_is_pre_execution=True,
            effect_type="read_only",error_protocol="tushare")

        class SelectDataset:
            def bind_tools(self,tools,*,tool_choice):
                class Bound:
                    def invoke(self,messages):
                        goal=json.loads(messages[-1].content)["task_goal"]
                        return AIMessage(content="",tool_calls=[{"id":"untrusted","type":"tool_call",
                            "name":"mcp_tushare_income_vip","args":{"dataset":goal}}])
                return Bound()

        client=SelectDataset()

        async def remote(_value):
            invoked.append(_value)
            return ToolMessage(content=[{"type":"text","text":
                f"tushare API 错误 [{provider_code}]: 任意语言的权限说明，不参与分类。"
            }],status="error",tool_call_id="remote")

        monkeypatch.setattr(type(adapter._langchain_tool),"ainvoke",lambda self,value:remote(value))
        registry=ToolRegistry()
        registry.register(adapter,execution_binding=McpGateway.execution_binding(
            McpServerConfig(name="tushare",url="https://provider.example/mcp",
                permission_denial_is_pre_execution=True,effect_type="read_only",error_protocol="tushare"),tool=adapter))
        registry.register(search)
        registry=registry.snapshot()
        worker=tool_agent.build_tool_agent_node(registry_factory=lambda:registry,llm_client=client,
            candidate_resolver=Resolver([registry.get(adapter.name).spec,registry.get("web_search").spec]))
        return worker,state,data,search,client,registry

    monkeypatch.setitem(globals(),"setup_worker",setup)
    graph,state,_,search,_,_=batch_graph(control,monkeypatch)
    state["intent"]={"source_constraint":constraint}
    state["request_flow_version"] = "context-routing-v1"
    state["planner_iteration"] = 1
    for task in state["plan"]["tasks"][:2]:
        task["source_requirement"] = requirement
    cfg={"configurable":{"thread_id":"real-text-recovery"}}
    with execution_control(control):
        finished=graph.invoke(state,cfg)
        assert "__interrupt__" not in finished
        assert len(invoked) == 2 and search.calls == 0
        assert not finished.get("recovery_batches")
        assert finished["plan"] == state["plan"]
        completed = {row["task_id"]: row for row in finished["task_results"]}
        assert completed["healthy"]["status"] == "success"
        expected = "provider_permission_denied" if provider_code == 2002 else "provider_request_rejected"
        assert all(completed[key]["error_code"] == expected for key in ("income", "cashflow"))
        assert len(finished["tool_selection_contexts"]) == 2
        with control.session_factory() as db:
            rows = list(db.scalars(select(AgentRunToolReceipt).where(
                AgentRunToolReceipt.run_id == control.run_id)))
            assert len(rows) == 2 and all(row.status == "settled" for row in rows)
            originals = [row for row in rows if row.tool_name != "web_search"]
            assert len(originals) == 2
            assert all(row.result_summary["execution_state"] == "not_started" for row in originals)


def confirm(payload, value="web_search"):
    return Command(
        resume={
            "interaction_id": payload["interaction_id"],
            "input_type": "selection",
            "value": value,
            "presentation_version": payload["recovery_batch"]["version"],
            "batch_digest": payload["recovery_batch"]["digest"],
        }
    )


def add_historical_unknown(control, *, session_key=None):
    from app.storage.models import AgentRunToolReceipt

    with control.session_factory() as db:
        current = db.get(AgentRun, control.run_id)
        db.add(AgentRun(
            run_id="historical-run", trace_id="historical-trace", status="completed",
            tenant_id=current.tenant_id, user_id=current.user_id,
            session_id=current.session_id, session_key=session_key or current.session_key,
        ))
        db.flush()
        db.add(AgentRunToolReceipt(
            receipt_id="historical-unknown", run_id="historical-run", owner_token="old-owner",
            tool_name="mcp.business.submit_order", call_id="historical-call", risk_level="medium",
            status="unknown", result_summary={"execution_state": "uncertain", "_effect_type": "write"},
        ))
        db.commit()


@pytest.mark.parametrize("choice", ["web_search", "use_existing"])
@pytest.mark.parametrize("requirement", ["public_information", "original_data"])
def test_historical_unknown_offers_frozen_search_without_clearing_or_replaying_original(
    control, monkeypatch, choice, requirement,
):
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_permission_fallback import DeniedTool

    monkeypatch.setattr(DeniedTool, "risk_level", "medium")
    # 此处复用假工具测试显式业务写契约；真实 Admin TuShare 连接始终只读。
    monkeypatch.setattr(DeniedTool, "effect_type", "write")
    graph, state, data, search, client, _ = batch_graph(control, monkeypatch)
    for task in state["plan"]["tasks"][:2]:
        task["source_requirement"] = requirement
    add_historical_unknown(control)
    cfg = {"configurable": {"thread_id": "historical-unknown-recovery"}}
    with execution_control(control):
        paused = graph.invoke(state, cfg)
        assert "__interrupt__" in paused, paused.get("task_results")
        assert len(paused["__interrupt__"]) == 1
        payload = paused["__interrupt__"][0].value
        assert len(payload["recovery_batch"]["items"]) == 2
        assert data.calls == search.calls == 0
        assert client.calls == 2
        assert "权限不足" not in payload["question"]
        assert all("未核实" in item["source_notice"] for item in payload["recovery_batch"]["items"])
        finished = graph.invoke(confirm(payload, choice), cfg)
    assert "__interrupt__" not in finished
    assert data.calls == 0 and client.calls == 2
    assert search.calls == (2 if choice == "web_search" else 0)
    assert any(r["task_id"] == "healthy" and r["status"] == "success" for r in finished["task_results"])
    from app.conversations.execution import UnresolvedToolEffect

    with pytest.raises(UnresolvedToolEffect):
        control.ensure_tool_effects_resolved(risk_level="medium", effect_type="write")
    with control.session_factory() as db:
        old = db.get(AgentRunToolReceipt, "historical-unknown")
        assert old.status == "unknown" and old.result_summary == {"execution_state": "uncertain", "_effect_type": "write"}
        current = list(db.scalars(select(AgentRunToolReceipt).where(AgentRunToolReceipt.run_id == control.run_id)))
        assert len(current) == (2 if choice == "web_search" else 0)
        assert all(r.tool_name == "web_search" and r.status == "settled" for r in current)


@pytest.mark.parametrize("restriction", ["original_only", "unclear", "no_candidate"])
def test_historical_unknown_cannot_offer_search_without_source_and_original_candidate(
    control, monkeypatch, restriction,
):
    from app.tests.unit.test_permission_fallback import DeniedTool

    monkeypatch.setattr(DeniedTool, "risk_level", "medium")
    # 此处复用假工具测试显式业务写契约；真实 Admin TuShare 连接始终只读。
    monkeypatch.setattr(DeniedTool, "effect_type", "write")
    if restriction == "no_candidate":
        original_setup = setup_worker
        monkeypatch.setitem(globals(), "setup_worker", lambda: original_setup(candidates=False))
    graph, state, data, search, _, _ = batch_graph(control, monkeypatch)
    add_historical_unknown(control)
    for task in state["plan"]["tasks"][:2]:
        task["source_requirement"] = "original_data"
    if restriction != "no_candidate":
        state["intent"]["source_constraint"] = restriction
    with execution_control(control):
        finished = graph.invoke(state, {"configurable": {"thread_id": "restricted-history"}})
    assert "__interrupt__" not in finished
    assert data.calls == search.calls == 0
    assert sum(r["status"] == "business_failure" for r in finished["task_results"]) == 2


@pytest.mark.parametrize("change", ["remote_receipt", "uncertain_result", "blocker_resolved"])
def test_local_unstarted_recovery_rechecks_execution_proof_before_confirmed_search(
    control, monkeypatch, change,
):
    from copy import deepcopy

    from app.graph.nodes.user_input_gate import UserInputResumeError
    from app.graph.recovery.batching import batch_request
    from app.schemas.recovery import RecoveryBatch
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_permission_fallback import DeniedTool

    monkeypatch.setattr(DeniedTool, "risk_level", "medium")
    # 此处复用假工具测试显式业务写契约；真实 Admin TuShare 连接始终只读。
    monkeypatch.setattr(DeniedTool, "effect_type", "write")
    graph, state, data, search, _, registry = batch_graph(control, monkeypatch)
    add_historical_unknown(control)
    with execution_control(control):
        paused = graph.invoke(state, {"configurable": {"thread_id": "proof-change"}})
        batch = RecoveryBatch.model_validate(next(iter(paused["recovery_batches"].values())))
        altered = deepcopy(paused)
        call_id = batch.items[0].original_call_id
        if change == "uncertain_result":
            for record in altered["tool_calls"]:
                if record.request.call_id == call_id:
                    record.result = record.result.model_copy(update={"execution_state": "uncertain"})
        else:
            with control.session_factory() as db:
                if change == "remote_receipt":
                    db.add(AgentRunToolReceipt(
                        receipt_id="unexpected-execution", run_id=control.run_id, owner_token=control.owner,
                        tool_name=batch.items[0].original_tool, call_id=call_id, risk_level="medium",
                        status="unknown", result_summary={"execution_state": "uncertain"},
                    ))
                else:
                    db.get(AgentRunToolReceipt, "historical-unknown").status = "settled"
                db.commit()
        with pytest.raises(UserInputResumeError):
            accept_confirmation(altered, batch_request(batch), "web_search", registry=registry)
    assert data.calls == search.calls == 0


def test_production_source_contract_reaches_interrupt_without_allowed_policy_stub(control, monkeypatch):
    graph, state, _, search, _, _ = batch_graph(control, monkeypatch)
    with execution_control(control):
        paused = graph.invoke(state, {"configurable": {"thread_id": "real-source-contract"}})
    assert "__interrupt__" in paused and search.calls == 0
    for context in paused["tool_selection_contexts"].values():
        assert context["source_policy"]["basis"][0] == "runtime-contract:public-supplement-v1"


def test_router_and_planner_keep_single_calls_without_source_gate(
    control, monkeypatch, business_intent_catalog,
):
    from app.graph.nodes.intent_router import build_intent_router_node
    from app.graph.nodes.planner_agent import build_planner_agent_node
    from app.tests.unit.test_dynamic_planner import RecordingLlmClient
    from app.tests.unit.test_intent_router import FakeLlmClient
    from app.tests.unit.test_permission_fallback import DeniedTool

    monkeypatch.setattr(DeniedTool, "effect_type", "read_only")
    graph, state, _, search, _, _ = batch_graph(control, monkeypatch)
    router_llm = FakeLlmClient({
        "intent_type": "financial_research", "domain": "a_share_equity_research",
        "subdomain": "financial_report_analysis", "goal": state["user_message"],
        "confidence": 0.95, "source_constraint": "unrestricted",
    })
    planner_llm = RecordingLlmClient(state["plan"])
    state["plan"] = None
    build_intent_router_node(catalog=business_intent_catalog, llm_client=router_llm)(state)
    build_planner_agent_node(llm_client=planner_llm)(state)
    with execution_control(control):
        paused = graph.invoke(state, {"configurable": {"thread_id": "existing-model-passes"}})
    assert "__interrupt__" not in paused and search.calls == 0
    assert sum(row["status"] == "business_failure" for row in paused["task_results"]) == 2
    assert len(router_llm.calls) == len(planner_llm.calls) == 1
    assert "source_constraint" not in paused["intent"]
    assert "source_constraint" not in json.loads(planner_llm.calls[0]["user_prompt"])["planning_constraints"]


@pytest.mark.parametrize("choice", ["web_search", "use_existing"])
def test_original_data_and_public_tasks_share_confirmation_without_changing_requirements(control, monkeypatch, choice):
    import json
    from copy import deepcopy

    graph, state, data, search, client, _ = batch_graph(control, monkeypatch)
    state["plan"]["tasks"][0]["source_requirement"] = "original_data"
    original_plan = deepcopy(state["plan"])
    config = {"configurable": {"thread_id": "mixed-source-contract"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        payload = paused["__interrupt__"][0].value
        assert len(payload["recovery_batch"]["items"]) == 2 and search.calls == 0
        assert data.calls == client.calls == 2
        finished = graph.invoke(confirm(payload, choice), config)
    results = {row["task_id"]: row for row in finished["task_results"]}
    assert finished["plan"] == original_plan
    assert results["income"]["status"] == results["cashflow"]["status"] == (
        "success" if choice == "web_search" else "business_failure"
    )
    assert results["healthy"]["status"] == "success"
    assert "原接口数据仍缺失" in json.loads(results["income"]["content"])["source_notice"]
    assert search.calls == (2 if choice == "web_search" else 0)
    assert data.calls == client.calls == 2


@pytest.mark.parametrize("restriction", ["original_only", "unclear", None])
def test_request_source_constraint_returns_gap_without_interrupt_or_external_search(
    control, monkeypatch, restriction,
):
    graph, state, _, search, _, _ = batch_graph(control, monkeypatch)
    state["intent"] = {} if restriction is None else {"source_constraint": restriction}
    with execution_control(control):
        finished = graph.invoke(state, {"configurable": {"thread_id": "restricted-request"}})
    assert "__interrupt__" not in finished and search.calls == 0
    assert sum(row["status"] == "business_failure" for row in finished["task_results"]) == 2
    assert any(row["task_id"] == "healthy" and row["status"] == "success" for row in finished["task_results"])


def test_source_deployment_revocation_after_confirmation_blocks_execution(control, monkeypatch):
    from app.core import config as app_config
    from app.graph.nodes.user_input_gate import UserInputResumeError

    graph, state, _, search, _, _ = batch_graph(control, monkeypatch)
    config = {"configurable": {"thread_id": "source-revoked"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        monkeypatch.setattr(app_config, "settings", app_config.settings.model_copy(
            update={"permission_recovery_source_policy": "unknown"}
        ))
        with pytest.raises(UserInputResumeError):
            graph.invoke(confirm(paused["__interrupt__"][0].value), config)
    assert search.calls == 0


def test_results_landed_before_checkpoint_failure_are_reused_without_remote_replay(
    control, monkeypatch
):
    failed = []

    def fail_once(_update):
        if not failed:
            failed.append(True)
            raise RuntimeError("controlled checkpoint window")

    graph, state, data, search, client, _ = batch_graph(
        control, monkeypatch, recovery_hook=fail_once
    )
    config = {"configurable": {"thread_id": "crash"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        with pytest.raises(RuntimeError, match="controlled checkpoint window"):
            graph.invoke(confirm(paused["__interrupt__"][0].value), config)
        assert search.calls == 2
        resumed = graph.invoke(None, config)
    assert search.calls == data.calls == client.calls == 2
    assert len(resumed["task_results"]) == 3


@pytest.mark.parametrize(
    "change", ["selection", "goal", "digest", "revoked", "source_policy", "account", "dispatch"]
)
def test_changed_authorization_is_rejected_before_search(control, monkeypatch, change):
    from copy import deepcopy

    from app.graph.nodes.user_input_gate import UserInputResumeError
    from app.graph.recovery.batching import batch_request
    from app.schemas.recovery import RecoveryBatch

    graph, state, _, search, _, registry = batch_graph(control, monkeypatch)
    with execution_control(control):
        paused = graph.invoke(state, {"configurable": {"thread_id": "tamper"}})
        altered = deepcopy(paused)
        batch = RecoveryBatch.model_validate(next(iter(paused["recovery_batches"].values())))
        first = batch.items[0]
        if change == "selection":
            altered["tool_selection_contexts"][first.original_call_id]["candidates"] = []
        elif change == "goal":
            altered["plan"]["tasks"][0]["task_goal"] = "更改目标"
        elif change == "digest":
            altered["recovery_batches"][batch.batch_id]["digest"] = "0" * 64
        elif change == "revoked":
            altered["permission_scopes"] = []
        elif change == "source_policy":
            altered["tool_selection_contexts"][first.original_call_id]["source_policy"][
                "public_alternative"
            ] = "forbidden"
        elif change == "account":
            altered["account_revision"] = "new-account"
        elif change == "dispatch":
            altered["dispatch_batches"][batch.dispatch_batch_id]["members"] = [first.task_id]
        with pytest.raises(UserInputResumeError):
            accept_confirmation(altered, batch_request(batch), "web_search", registry=registry)
    assert search.calls == 0


def test_confirmed_batch_cannot_reverse_choice(control, monkeypatch):
    from app.graph.nodes.user_input_gate import UserInputResumeError
    from app.graph.recovery.batching import batch_request
    from app.schemas.recovery import RecoveryBatch

    graph, state, _, search, _, registry = batch_graph(control, monkeypatch)
    with execution_control(control):
        paused = graph.invoke(state, {"configurable": {"thread_id": "reverse"}})
        batch = RecoveryBatch.model_validate(next(iter(paused["recovery_batches"].values())))
        request = batch_request(batch)
        accept_confirmation(paused, request, "use_existing", registry=registry)
        accept_confirmation(paused, request, "use_existing", registry=registry)
        with pytest.raises(UserInputResumeError) as exc:
            accept_confirmation(paused, request, "web_search", registry=registry)
    assert exc.value.code == "permission_recovery_decision_conflict"
    assert search.calls == 0


def test_medium_risk_healthy_call_waits_for_peer_refusal_settlement_without_disabling_guard(
    control, monkeypatch
):
    import asyncio
    from threading import Event, Lock

    from app.conversations.execution import ExecutionControl
    from app.schemas.tools import ToolCallResult
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_permission_fallback import DeniedTool

    second_guard = Event()
    lock, checks, remote_attempts = Lock(), [], []
    original_guard, original_run = ExecutionControl.ensure_tool_effects_resolved, DeniedTool.run

    def guard(self, **kwargs):
        if kwargs.get("db") is not None and kwargs["risk_level"] == "medium":
            with lock:
                checks.append(kwargs["call_id"])
                if len(checks) >= 2:
                    second_guard.set()
        return original_guard(self, **kwargs)

    async def run(self, input_data):
        with lock:
            remote_attempts.append(True)
            first = len(remote_attempts) == 1
        if first:
            assert await asyncio.to_thread(second_guard.wait, 3)
            return await original_run(self, input_data)
        self.calls += 1
        return ToolCallResult(
            tool_call_id="result",
            tool_name=self.name,
            status="success",
            trace_id="",
            result={"data": [1, 2, 3]},
        )

    monkeypatch.setattr(ExecutionControl, "ensure_tool_effects_resolved", guard)
    monkeypatch.setattr(DeniedTool, "risk_level", "medium")
    # 此处复用假工具测试显式业务写契约；真实 Admin TuShare 连接始终只读。
    monkeypatch.setattr(DeniedTool, "effect_type", "write")
    monkeypatch.setattr(DeniedTool, "run", run)
    graph, state, data, search, client, _ = batch_graph(control, monkeypatch)
    with execution_control(control):
        paused = graph.invoke(state, {"configurable": {"thread_id": "healthy-medium"}})
    assert data.calls == 2 and client.calls == 2 and search.calls == 0
    assert len(paused["__interrupt__"][0].value["recovery_batch"]["items"]) == 1
    assert sum(row["status"] == "success" for row in paused["task_results"]) == 2
    with control.session_factory() as db:
        receipts = list(
            db.scalars(
                select(AgentRunToolReceipt).where(AgentRunToolReceipt.run_id == control.run_id)
            )
        )
        assert len(receipts) == 2 and all(row.status == "settled" for row in receipts)


@pytest.mark.parametrize("permission_denied", [False, True])
def test_three_medium_calls_do_not_drop_a_second_deferred_attempt(
    control, monkeypatch, permission_denied,
):
    """首轮两个等待项；重派时不能再互相碰撞并丢掉其中一个。"""
    import asyncio
    from threading import Event, Lock

    from app.conversations.execution import ExecutionControl, UnresolvedToolEffect
    from app.schemas.tools import ToolCallResult, ToolError
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_permission_fallback import DeniedTool

    initial_waiters, repeated_collision = Event(), Event()
    lock, blocked = Lock(), []
    original_guard = ExecutionControl.ensure_tool_effects_resolved

    def guard(self, **kwargs):
        try:
            return original_guard(self, **kwargs)
        except UnresolvedToolEffect:
            if kwargs.get("db") is not None:
                with lock:
                    blocked.append(kwargs.get("call_id"))
                    if len(blocked) >= 2:
                        initial_waiters.set()
                    if len(blocked) >= 3:
                        repeated_collision.set()
            raise

    async def run(self, input_data):
        with lock:
            self.calls += 1
            attempt = self.calls
        if attempt == 1:
            assert await asyncio.to_thread(initial_waiters.wait, 3)
        elif attempt == 2:
            # 旧实现会并发重派两个等待项，确保其二次碰撞发生在本次结果落盘之前。
            # 修复后只派发一个，这里超时继续即可，不要求制造不存在的碰撞。
            await asyncio.to_thread(repeated_collision.wait, 1)
        return ToolCallResult(
            tool_call_id=f"result_{attempt}", tool_name=self.name, trace_id="",
            status="denied" if permission_denied else "success",
            execution_state="not_started" if permission_denied else "completed",
            result=None if permission_denied else {"data": [attempt]},
            error=(ToolError(code="provider_permission_denied", message="权限不足")
                if permission_denied else None),
        )

    monkeypatch.setattr(ExecutionControl, "ensure_tool_effects_resolved", guard)
    monkeypatch.setattr(DeniedTool, "risk_level", "medium")
    # 此处复用假工具测试显式业务写契约；真实 Admin TuShare 连接始终只读。
    monkeypatch.setattr(DeniedTool, "effect_type", "write")
    monkeypatch.setattr(DeniedTool, "run", run)
    graph, state, data, search, client, _ = batch_graph(
        control, monkeypatch, healthy_uses_gateway=True,
    )
    state["plan"]["tasks"][2]["source_requirement"] = "public_information"
    config = {"configurable": {"thread_id": "three-medium"}}
    with execution_control(control):
        finished = graph.invoke(state, config)
        assert data.calls == client.calls == 3
        if permission_denied:
            payload = finished["__interrupt__"][0].value
            assert len(payload["recovery_batch"]["items"]) == 3 and search.calls == 0
            finished = graph.invoke(confirm(payload), config)
            assert search.calls == 3
        else:
            assert "__interrupt__" not in finished and search.calls == 0
    assert all(row["status"] == "success" for row in finished["task_results"])
    assert len(finished["task_results"]) == 3
    assert data.calls == client.calls == 3
    assert len(blocked) == 2
    with control.session_factory() as db:
        receipts = list(db.scalars(select(AgentRunToolReceipt).where(
            AgentRunToolReceipt.run_id == control.run_id,
            AgentRunToolReceipt.tool_name == data.name,
        )))
        assert len(receipts) == 3 and all(row.status == "settled" for row in receipts)


def test_budget_is_shared_with_prior_searches_and_consumed_in_stable_batch_order(
    control, monkeypatch
):
    from app.tests.unit.test_recovery_durability import search_request, success

    graph, state, _, search, _, registry = batch_graph(control, monkeypatch)
    for i in range(5):
        claim = control.claim_search_invocation(
            search_request(f"prior-{i}"),
            binding_digest=registry.get("web_search").execution_binding_digest,
            budget_limit=6,
            existing_call_ids=(),
        )
        control.tool_finished(claim.receipt_id, success())
    config = {"configurable": {"thread_id": "budget"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        resumed = graph.invoke(confirm(paused["__interrupt__"][0].value), config)
    assert search.calls == 1
    by_task = {item["task_id"]: item for item in resumed["task_results"]}
    assert by_task["cashflow"]["status"] == "success"
    assert by_task["income"]["error_code"] == "research_budget_exhausted"
    assert "__interrupt__" not in resumed


def test_unresolved_search_is_not_redispatched_and_other_authorized_item_continues(
    control, monkeypatch
):
    from app.graph.recovery.executor import recovery_search_request
    from app.schemas.recovery import RecoveryBatch

    graph, state, _, search, _, registry = batch_graph(control, monkeypatch)
    config = {"configurable": {"thread_id": "unresolved"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        batch = RecoveryBatch.model_validate(next(iter(paused["recovery_batches"].values())))
        request = recovery_search_request(batch=batch, item=batch.items[0])
        assert (
            control.claim_search_invocation(
                request,
                binding_digest=registry.get("web_search").execution_binding_digest,
                budget_limit=6,
                existing_call_ids=(),
            ).disposition
            == "execute"
        )
        resumed = graph.invoke(confirm(paused["__interrupt__"][0].value), config)
    assert search.calls == 1
    assert (
        next(row for row in resumed["task_results"] if row["task_id"] == batch.items[0].task_id)[
            "error_code"
        ]
        == "search_invocation_unresolved"
    )
    assert "__interrupt__" not in resumed


def test_one_search_failure_does_not_stop_other_authorized_items_or_reopen_confirmation(
    control, monkeypatch
):
    from app.schemas.tools import ToolCallResult, ToolError
    from app.tests.unit.test_permission_fallback import SearchTool

    original = SearchTool.run

    async def search_run(self, input_data):
        if self.calls == 0:
            self.calls += 1
            return ToolCallResult(
                tool_call_id="result",
                tool_name=self.name,
                status="failed",
                trace_id="",
                execution_state="completed",
                error=ToolError(code="web_search_no_results", message="无结果"),
            )
        return await original(self, input_data)

    monkeypatch.setattr(SearchTool, "run", search_run)
    graph, state, _, search, client, _ = batch_graph(control, monkeypatch)
    config = {"configurable": {"thread_id": "partial-failure"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        resumed = graph.invoke(confirm(paused["__interrupt__"][0].value), config)
    assert search.calls == client.calls == 2
    assert sum(row["status"] == "success" for row in resumed["task_results"]) == 2
    assert "__interrupt__" not in resumed and "planner_stop_reason" not in resumed


@pytest.mark.parametrize(
    "policy",
    [
        {"public_alternative": "unknown", "basis": []},
        {"public_alternative": "forbidden", "basis": ["test:user-source-constraint"]},
        {"public_alternative": "allowed", "basis": []},
    ],
)
def test_missing_or_forbidden_source_contract_leaves_gap_not_silent_search(
    control, monkeypatch, policy
):
    graph, state, _, search, _, _ = batch_graph(control, monkeypatch, policy=policy)
    with execution_control(control):
        finished = graph.invoke(state, {"configurable": {"thread_id": "unknown-source"}})
    assert "__interrupt__" not in finished and search.calls == 0
    assert [
        row["error_code"] for row in finished["task_results"] if row["status"] != "success"
    ] == ["source_policy_unavailable"] * 2


def test_recovery_uses_original_research_time_and_not_resume_date(control, monkeypatch):
    from app.tests.unit.test_permission_fallback import SearchTool

    contexts = []

    async def run_with_context(self, input_data, *, context):
        contexts.append(context)
        return await self.run(input_data)

    monkeypatch.setattr(SearchTool, "run_with_context", run_with_context, raising=False)
    graph, state, _, _, _, _ = batch_graph(control, monkeypatch)
    state["research_as_of"] = "2020-01-01T10:00:00+08:00"
    config = {"configurable": {"thread_id": "as-of"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        graph.invoke(confirm(paused["__interrupt__"][0].value), config)
    assert len(contexts) == 2
    assert all(context.research_context.as_of == state["research_as_of"] for context in contexts)


def test_production_resume_rejects_revocation_before_invoke(control, monkeypatch):
    from app.graph import runtime
    from app.graph.state import build_checkpoint_thread_id

    graph, state, _, search, _, registry = batch_graph(control, monkeypatch)
    thread_id = build_checkpoint_thread_id(
        tenant_id=state["tenant_id"],
        session_key=state["session_key"],
        request_id=state["request_id"],
    )
    config = {"configurable": {"thread_id": thread_id}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        interrupted = paused["__interrupt__"][0]
        payload = interrupted.value
        monkeypatch.setattr(runtime, "build_question_answer_graph", lambda **kwargs: graph)
        with pytest.raises(runtime.AgentGraphExecutionError) as exc:
            runtime.resume_user_input_graph(
                business_intent_catalog=object(),
                checkpointer=None,
                checkpoint_thread_id=thread_id,
                interrupt_id=interrupted.id,
                interaction_id=payload["interaction_id"],
                input_type="selection",
                value="web_search",
                presentation_version=payload["recovery_batch"]["version"],
                batch_digest=payload["recovery_batch"]["digest"],
                permission_scopes=[],
                tool_registry_factory=lambda: registry,
            )
    assert (
        exc.value.http_status == 409 and exc.value.code == "permission_recovery_permission_revoked"
    )
    assert search.calls == 0


def test_existing_uncertain_effect_remains_protected_and_is_not_rewritten(control, monkeypatch):
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_permission_fallback import DeniedTool

    monkeypatch.setattr(DeniedTool, "risk_level", "medium")
    # 此处复用假工具测试显式业务写契约；真实 Admin TuShare 连接始终只读。
    monkeypatch.setattr(DeniedTool, "effect_type", "write")
    receipt = control.tool_started(
        ToolCallRequest(
            call_id="controlled-unknown",
            tool_name="mcp.test.write",
            arguments={},
            purpose="受控未决执行",
        ),
        risk_level="medium",
        effect_type="write",
    )
    control.tool_finished(
        receipt, {"status": "unknown", "error": {"code": "controlled_timeout"}}, uncertain=True
    )
    with control.session_factory() as db:
        before = dict(db.get(AgentRunToolReceipt, receipt).result_summary)
    graph, state, data, search, client, _ = batch_graph(control, monkeypatch)
    with execution_control(control):
        cfg = {"configurable": {"thread_id": "real-unknown"}}
        paused = graph.invoke(state, cfg)
        assert "__interrupt__" in paused
        finished = graph.invoke(confirm(paused["__interrupt__"][0].value, "use_existing"), cfg)
    assert data.calls == search.calls == 0 and client.calls == 2
    assert "__interrupt__" not in finished
    assert [
        row["error_code"] for row in finished["task_results"] if row["status"] != "success"
    ] == ["permission_recovery_declined"] * 2
    with control.session_factory() as db:
        original = db.get(AgentRunToolReceipt, receipt)
        assert original.status == "unknown" and original.result_summary == before


def test_new_task_has_new_batch_and_cannot_inherit_the_old_confirmation(control, monkeypatch):
    from app.graph.planning.contracts import PlanModel
    from app.graph.planning.policy import observation_signature

    graph, state, data, search, client, _ = batch_graph(control, monkeypatch)
    config = {"configurable": {"thread_id": "later"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        first = paused["__interrupt__"][0].value
        finished = graph.invoke(confirm(first), config)
        new_plan = {
            "tasks": [*state["plan"]["tasks"], {"id": "later", "task_goal": "查询新的公开资料",
                "source_requirement": "public_information"}]
        }
        next_pause = graph.invoke(
            {
                **finished,
                "plan": new_plan,
                "planner_decision": {"action": "continue"},
                "planner_reviewed_signature": observation_signature(
                    PlanModel.model_validate(new_plan), finished["task_results"]
                ),
            },
            config,
        )
    second = next_pause["__interrupt__"][0].value
    assert first["interaction_id"] != second["interaction_id"]
    assert first["recovery_batch"]["batch_id"] != second["recovery_batch"]["batch_id"]
    assert (
        len(first["recovery_batch"]["items"]) == 2 and len(second["recovery_batch"]["items"]) == 1
    )
    assert data.calls == client.calls == 3 and search.calls == 2


def test_real_recovery_trace_links_calls_and_does_not_duplicate_private_inputs(
    control, monkeypatch, telemetry
):
    import json

    from app.operations.langfuse_observability import langfuse_observation

    runtime, exporter = telemetry
    graph, state, _, search, _, _ = batch_graph(control, monkeypatch)
    state["user_message"] = "synthetic-private-query"
    config = {"configurable": {"thread_id": "telemetry"}}
    with (
        execution_control(control),
        langfuse_observation(as_type="span", name="controlled_recovery_test"),
    ):
        paused = graph.invoke(state, config)
        graph.invoke(confirm(paused["__interrupt__"][0].value), config)
    runtime.client.flush()
    runtime.provider.force_flush()
    events = [
        span
        for span in exporter.get_finished_spans()
        if span.name
        in {
            "result_classified",
            "receipt_settled",
            "recovery_batch_sealed",
            "recovery_decision",
            "recovery_dispatch",
            "recovery_result",
        }
    ]
    names = {span.name for span in events}
    assert names == {
        "result_classified",
        "receipt_settled",
        "recovery_batch_sealed",
        "recovery_decision",
        "recovery_dispatch",
        "recovery_result",
    }
    payloads = [json.loads(span.attributes["langfuse.observation.output"]) for span in events]
    dispatches = [payload for payload in payloads if payload["event"] == "recovery_dispatch"]
    assert len(dispatches) == search.calls == 2
    assert all(
        payload["budget_disposition"] == "execute" and payload["batch_id"] for payload in dispatches
    )
    assert "synthetic-private-query" not in json.dumps(payloads)
    assert all("query" not in payload and "answer" not in payload for payload in payloads)


def test_observer_failure_preserves_receipts_and_recovery_results(control, monkeypatch):
    from app.operations import langfuse_observability
    from app.storage.models import AgentRunToolReceipt

    def broken(**kwargs):
        raise RuntimeError("controlled-observer-failure")

    monkeypatch.setattr(langfuse_observability, "langfuse_observation", broken)
    graph, state, data, search, _, _ = batch_graph(control, monkeypatch)
    config = {"configurable": {"thread_id": "observer-failure"}}
    with execution_control(control):
        paused = graph.invoke(state, config)
        finished = graph.invoke(confirm(paused["__interrupt__"][0].value), config)
    assert data.calls == search.calls == 2 and len(finished["task_results"]) == 3
    with control.session_factory() as db:
        assert all(row.status == "settled" for row in db.scalars(select(AgentRunToolReceipt)))


def test_recovery_does_not_reselect_tools_and_preserves_one_normal_planner_review(
    control, monkeypatch
):
    from app.graph.nodes.planner_agent import build_planner_agent_node
    from app.llm.schemas import LlmCompletionResult

    class Review:
        calls = 0

        async def complete(self, **kwargs):
            self.calls += 1
            return LlmCompletionResult(
                content='{"action":"finish","reason":"依据已有材料与缺口结束"}',
                model="fake",
                provider="fake",
            )

    review = Review()
    graph, state, data, search, client, _ = batch_graph(
        control, monkeypatch, planner_node=build_planner_agent_node(llm_client=review)
    )
    state["intent"] = {
        "intent_type": "financial_research",
        "domain": "equity",
        "subdomain": "company",
        "goal": "依据材料回答",
        "source_constraint": "unrestricted",
    }
    config = {"configurable": {"thread_id": "one-review"}}
    with execution_control(control):
        paused = graph.invoke(state, config)

        def no_reselection(*args, **kwargs):
            raise AssertionError("恢复不得调用工具选择模型")

        monkeypatch.setattr(client, "bind_tools", no_reselection)
        finished = graph.invoke(confirm(paused["__interrupt__"][0].value), config)
    assert review.calls == 1 and data.calls == search.calls == 2
    assert finished["planner_decision"]["action"] == "finish"


def test_each_recovered_search_emits_its_own_public_answer(control, monkeypatch):
    from app.tools.web_search import WebSearchTool

    class SummaryClient(RecordingSearchClient):
        async def search(self, **kwargs):
            result = await super().search(**kwargs)
            return result.model_copy(update={"answer": "摘要：" + kwargs["query"]})

    search_client = SummaryClient()
    graph, state, _, _, _, _ = batch_graph(
        control, monkeypatch, search_tool=WebSearchTool(client=search_client),
    )
    graph_config = {"configurable": {"thread_id": "recovery-public-answers"}}
    with execution_control(control):
        paused = graph.invoke(state, graph_config)
        payload = paused["__interrupt__"][0].value
        events = list(graph.stream(Command(resume={
            "interaction_id": payload["interaction_id"], "input_type": "selection", "value": "web_search",
            "presentation_version": payload["recovery_batch"]["version"],
            "batch_digest": payload["recovery_batch"]["digest"],
        }), graph_config, stream_mode="custom"))
    progress = [event for event in events if event.get("type") == "progress"]
    assert len(progress) == 4
    for task_id in ("income", "cashflow"):
        assert [event["status"] for event in progress if event["task_id"] == task_id] == ["started", "completed"]
    assert all(event.get("answer", "").startswith("摘要：") for event in progress if event["status"] == "completed")
    assert all("answer" not in event for event in progress if event["status"] == "started")
