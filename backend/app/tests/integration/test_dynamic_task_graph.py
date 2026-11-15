import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from app.graph.runtime import run_question_answer_graph
from app.llm.deepseek import LlmProviderError
from app.llm.schemas import LlmCompletionResult
from app.schemas.tools import ToolCallResult
from app.tools.base import Tool
from app.tools.registry import ToolRegistry
from app.tools.search import ToolSearchIndex, ToolSearchService
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel


def test_graph_run_attaches_langfuse_callback_inside_root_context(monkeypatch):
    from app.graph import runtime as runtime_module

    callback = object()
    active = []
    captured = {}
    finalized = []
    state = {
        "trace_id": "trace-1",
        "session_key": "session-key-1",
        "tenant_id": "tenant-1",
        "final_response": {"content": "hello back"},
    }

    class Graph:
        def invoke(self, value, *, config):
            assert active == [True]
            assert value is state
            captured["config"] = config
            return value

    class Handle:
        def __iter__(self):
            return iter((callback,))

        def update(self, **kwargs):
            captured["final"] = kwargs

    @contextmanager
    def agent_run(**kwargs):
        captured["context"] = kwargs
        active.append(True)
        try:
            yield Handle()
        finally:
            active.pop()

    monkeypatch.setattr(
        runtime_module,
        "_prepare_question_answer_run",
        lambda **_kwargs: (state, Graph(), {"recursion_limit": 8}),
    )
    monkeypatch.setattr(runtime_module, "langfuse_agent_run", agent_run)
    def finalize(handle, result, expected=None):
        handle.update(output=result["final_response"])
        finalized.append((handle, result, expected))

    monkeypatch.setattr(
        runtime_module,
        "_active_langfuse_finalizer",
        SimpleNamespace(finalize=finalize),
    )

    result = runtime_module.run_question_answer_graph(
        business_intent_catalog=object(),
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="hello",
    )

    assert result is state
    assert captured["config"]["callbacks"] == [callback]
    assert captured["context"] == {
        "business_trace_id": "trace-1",
        "session_key": "session-key-1",
        "tenant_id": "tenant-1",
        "execution_segment": "initial",
        "user_id": "user-1",
        "input": "hello",
    }
    assert captured["final"]["output"] == {"content": "hello back"}
    assert len(finalized) == 1
    assert finalized[0][1:] == (state, None)
    assert active == []


def test_graph_run_finalizes_safe_failure_snapshot_before_reraising(monkeypatch):
    from app.graph import runtime as runtime_module

    finalized = []
    state = {
        "trace_id": "trace-failed",
        "request_id": "request-failed",
        "session_key": "session-key-failed",
        "tenant_id": "tenant-failed",
        "user_message": "question",
    }

    class Graph:
        def invoke(self, _value, *, config):
            raise RuntimeError("private failure sk-secret")

    class Handle:
        def __iter__(self):
            return iter(())

    @contextmanager
    def agent_run(**_kwargs):
        yield Handle()

    monkeypatch.setattr(
        runtime_module,
        "_prepare_question_answer_run",
        lambda **_kwargs: (state, Graph(), {"recursion_limit": 8}),
    )
    monkeypatch.setattr(runtime_module, "langfuse_agent_run", agent_run)
    monkeypatch.setattr(
        runtime_module,
        "_active_langfuse_finalizer",
        SimpleNamespace(finalize=lambda _handle, snapshot: finalized.append(snapshot)),
    )

    with pytest.raises(RuntimeError, match="private failure"):
        runtime_module.run_question_answer_graph(
            business_intent_catalog=object(),
            session_id="session-failed",
            tenant_id="tenant-failed",
            user_id="user-failed",
            user_message="question",
        )

    assert finalized[0]["trace_id"] == "trace-failed"
    assert finalized[0]["errors"] == [{
        "node": "graph",
        "code": "agent_graph_execution_failed",
        "http_status": 500,
        "handled": False,
        "retryable": False,
    }]
    assert "private failure" not in repr(finalized[0])
    assert "sk-secret" not in repr(finalized[0])


def test_graph_stream_keeps_langfuse_root_context_open(monkeypatch):
    from app.graph import runtime as runtime_module

    callback = object()
    active = []
    finalized = []
    state = {
        "trace_id": "trace-2",
        "session_key": "session-key-2",
        "tenant_id": "tenant-2",
        "final_response": {"content": "done"},
    }

    class Graph:
        def stream(self, value, *, config, stream_mode):
            assert active == [True]
            assert config["callbacks"] == [callback]
            assert stream_mode == ["updates", "messages", "custom"]
            yield "updates", {"node": {"request_id": "request-2", "final_response": {"content": "done"}}}

    class Handle:
        def __iter__(self):
            return iter((callback,))

        def update(self, **kwargs):
            state["langfuse_final"] = kwargs

    @contextmanager
    def agent_run(**_kwargs):
        active.append(True)
        try:
            yield Handle()
        finally:
            active.pop()

    monkeypatch.setattr(
        runtime_module,
        "_prepare_question_answer_run",
        lambda **_kwargs: (state, Graph(), {"recursion_limit": 8}),
    )
    monkeypatch.setattr(runtime_module, "langfuse_agent_run", agent_run)
    def finalize(handle, result, expected=None):
        handle.update(output=result["final_response"])
        finalized.append((result, expected))

    monkeypatch.setattr(
        runtime_module,
        "_active_langfuse_finalizer",
        SimpleNamespace(finalize=finalize),
    )

    updates = list(
        runtime_module.stream_question_answer_graph(
            business_intent_catalog=object(),
            session_id="session-2",
            tenant_id="tenant-2",
            user_id="user-2",
            user_message="hello",
        )
    )

    assert updates[0]["final_response"] == {"content": "done"}
    assert state["langfuse_final"]["output"] == {"content": "done"}
    assert finalized == [(updates[0], None)]
    assert active == []


def test_graph_resume_reuses_business_trace_and_marks_resume_segment(monkeypatch):
    from app.graph import runtime as runtime_module

    callback = object()
    captured = {}
    finalized = []
    snapshot = SimpleNamespace(
        values={
            "trace_id": "trace-resume",
            "session_key": "session-key-resume",
            "tenant_id": "tenant-resume",
            "user_id": "user-resume",
            "messages": [HumanMessage(content="继续分析")],
            "final_response": {"content": "恢复完成"},
            "planner_policy_version": runtime_module.PLANNER_POLICY_VERSION,
            "tool_catalog_digest": "catalog-1",
        },
        tasks=(),
    )

    class Graph:
        def get_state(self, _config):
            return snapshot

        def invoke(self, command, *, config):
            captured["config"] = config
            captured["command"] = command
            return snapshot.values

    class Handle:
        def __iter__(self):
            return iter((callback,))

        def update(self, **kwargs):
            captured["final"] = kwargs

    @contextmanager
    def agent_run(**kwargs):
        captured["context"] = kwargs
        yield Handle()

    monkeypatch.setattr(runtime_module, "build_question_answer_graph", lambda **_kwargs: Graph())
    monkeypatch.setattr(
        runtime_module,
        "_runtime_registry_snapshot",
        lambda **_kwargs: SimpleNamespace(catalog_digest="catalog-1"),
    )
    monkeypatch.setattr(
        runtime_module,
        "_find_interrupt_payload",
        lambda *_args, **_kwargs: {
            "approval_id": "approval-1",
            "action_digest": "digest-1",
        },
    )
    monkeypatch.setattr(runtime_module, "langfuse_agent_run", agent_run)
    def finalize(handle, result, expected=None):
        handle.update(output=result["final_response"])
        finalized.append((result, expected))

    monkeypatch.setattr(
        runtime_module,
        "_active_langfuse_finalizer",
        SimpleNamespace(finalize=finalize),
    )

    result = runtime_module.resume_question_answer_graph(
        business_intent_catalog=object(),
        checkpoint_thread_id="cpr1_" + "a" * 64,
        checkpoint_id="checkpoint-1",
        interrupt_id="interrupt-1",
        action_digest="digest-1",
        approval_decision={"approval_id": "approval-1"},
        checkpointer=object(),
        tool_registry_factory=lambda: object(),
    )

    assert result is snapshot.values
    assert captured["config"]["callbacks"] == [callback]
    assert captured["context"] == {
        "business_trace_id": "trace-resume",
        "session_key": "session-key-resume",
        "tenant_id": "tenant-resume",
        "execution_segment": "resume",
        "user_id": "user-resume",
        "input": "继续分析",
    }
    assert captured["final"]["output"] == {"content": "恢复完成"}
    assert finalized == [(snapshot.values, None)]


@pytest.mark.parametrize("stream_behavior", ["interrupt", "error", "early_close"])
def test_graph_stream_does_not_record_incomplete_final_output(monkeypatch, stream_behavior):
    from app.graph import runtime as runtime_module

    captured = {}
    finalized = []
    state = {
        "trace_id": "trace-incomplete",
        "session_key": "session-key-incomplete",
        "tenant_id": "tenant-incomplete",
    }

    class Graph:
        def stream(self, _value, *, config, stream_mode):
            if stream_behavior == "interrupt":
                yield "updates", {"__interrupt__": ["approval"]}
                return
            yield "updates", {
                "node": {"request_id": "request-incomplete", "final_response": {"content": "partial"}}
            }
            if stream_behavior == "error":
                raise RuntimeError("stream stopped")

    class Handle:
        def __iter__(self):
            return iter(())

        def update(self, **kwargs):
            captured["final"] = kwargs

    @contextmanager
    def agent_run(**_kwargs):
        yield Handle()

    monkeypatch.setattr(
        runtime_module,
        "_prepare_question_answer_run",
        lambda **_kwargs: (state, Graph(), {"recursion_limit": 8}),
    )
    monkeypatch.setattr(runtime_module, "langfuse_agent_run", agent_run)
    monkeypatch.setattr(
        runtime_module,
        "_active_langfuse_finalizer",
        SimpleNamespace(
            finalize=lambda _handle, snapshot, **_kwargs: finalized.append(snapshot)
        ),
    )

    stream = runtime_module.stream_question_answer_graph(
        business_intent_catalog=object(),
        session_id="session-incomplete",
        tenant_id="tenant-incomplete",
        user_id="user-incomplete",
        user_message="hello",
    )
    if stream_behavior == "error":
        with pytest.raises(RuntimeError, match="stream stopped"):
            list(stream)
    elif stream_behavior == "early_close":
        next(stream)
        stream.close()
    else:
        list(stream)

    assert "final" not in captured
    if stream_behavior == "error":
        assert finalized[0]["trace_id"] == "trace-incomplete"
        assert finalized[0]["errors"][0]["code"] == "agent_graph_execution_failed"
    else:
        assert finalized == []


class WorkflowLlm:
    async def complete(self, **kwargs):
        prompt = kwargs["system_prompt"]
        if "投研任务理解器" in prompt:
            content = json.dumps(
                {
                    "intent_type": "financial_research",
                    "domain": "a_share_equity_research",
                    "subdomain": "company_snapshot",
                    "goal": "分析海能达的经营质量",
                    "confidence": 0.95,
                },
                ensure_ascii=False,
            )
        elif "计划反馈决策器" in prompt:
            content = json.dumps(
                {"action": "finish", "reason": "完成本次研究，保留证据限制"}, ensure_ascii=False
            )
        elif "投研任务 Planner" in prompt:
            content = json.dumps(
                {
                    "tasks": [
                        {
                            "id": "quality",
                            "task_goal": "取得评估经营质量所需财务事实",
                            "depends_on": [],
                            "skill_name": None,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        elif "投研任务执行器" in prompt:
            content = "当前没有外部经营数据，无法判断经营质量。"
        elif "投研报告合成器" in prompt:
            content = "## 分析结论\n\n现有数据不足，暂时无法判断海能达的经营质量。"
        else:
            raise AssertionError(prompt)
        return LlmCompletionResult(content=content, model="fake", provider="fake")


class MarketDataInput(BaseModel):
    symbol: str


class MarketDataTool(Tool[MarketDataInput]):
    name = "mcp.tushare.stock.daily"
    version = "1.0.0"
    description = "根据证券代码查询 A 股日线行情"
    input_model = MarketDataInput

    async def run(self, input_data: MarketDataInput) -> ToolCallResult:
        return ToolCallResult(
            tool_call_id="provider-call",
            tool_name=self.name,
            status="success",
            result={"symbol": input_data.symbol, "close": 8.12, "pct_chg": 1.5},
            trace_id="provider-trace",
        )


class BoundMarketModel:
    def invoke(self, _messages):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "model-call",
                    "name": "mcp_tushare_stock_daily",
                    "args": {"symbol": "002583.SZ"},
                    "type": "tool_call",
                }
            ],
        )


class ToolWorkflowLlm:
    def bind_tools(self, tools, *, tool_choice):
        assert tools[0]["function"]["name"] == "mcp_tushare_stock_daily"
        assert tool_choice == "auto"
        return BoundMarketModel()

    async def complete(self, **kwargs):
        prompt = kwargs["system_prompt"]
        if "投研任务理解器" in prompt:
            content = json.dumps(
                {
                    "intent_type": "financial_research",
                    "domain": "a_share_equity_research",
                    "subdomain": "company_snapshot",
                    "goal": "查询海能达最新行情并解读涨跌",
                    "confidence": 0.98,
                },
                ensure_ascii=False,
            )
        elif "计划反馈决策器" in prompt:
            content = json.dumps(
                {"action": "finish", "reason": "完成本次研究，保留证据限制"}, ensure_ascii=False
            )
        elif "投研任务 Planner" in prompt:
            assert "tools" not in json.loads(kwargs["user_prompt"])
            content = json.dumps(
                {
                    "tasks": [
                        {
                            "id": "market_data",
                            "task_goal": "查询海能达最新日线行情",
                            "depends_on": [],
                            "skill_name": None,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        elif "投研报告合成器" in prompt:
            research_materials = json.loads(kwargs["user_prompt"])["research_materials"]
            assert research_materials[0]["content"]["close"] == 8.12
            content = "## 行情解读\n\n海能达收盘价为 8.12 元，当日上涨 1.5%。"
        else:
            raise AssertionError(prompt)
        return LlmCompletionResult(content=content, model="fake", provider="fake")


def _market_tool_runtime():
    registry = ToolRegistry()
    registry.register(MarketDataTool())
    snapshot = registry.snapshot()
    index = ToolSearchIndex.build(
        specs=snapshot.specs(),
        catalog_digest=snapshot.catalog_digest,
    )
    search_service = ToolSearchService(
        registry=snapshot,
        index=index,
        candidate_k=20,
    )
    return snapshot, search_service


def test_full_graph_collects_evidence_then_synthesizes_analysis(business_intent_catalog):
    registry = ToolRegistry().snapshot()

    result = run_question_answer_graph(
        business_intent_catalog=business_intent_catalog,
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析海能达的经营质量",
        skill_agent_node=lambda state: state,
        tool_agent_node=lambda state: {
            "task_results": [
                {
                    "task_id": state["active_task"]["id"],
                    "status": "success",
                    "content": "当前没有外部经营数据，无法判断经营质量。",
                }
            ]
        },
        allow_test_tool_agent_override=True,
        tool_registry_factory=lambda: registry,
        llm_client=WorkflowLlm(),
    )

    assert result["task_results"][0]["task_id"] == "quality"
    assert result["task_results"][0]["status"] == "success"
    assert result["verification"]["verdict"] == "pass"
    assert "## 分析结论" in result["final_response"]["content"]
    assert "不构成投资建议" in result["final_response"]["content"]


def test_planner_llm_timeout_returns_safe_retryable_response(business_intent_catalog):
    class PlannerTimeoutLlm(WorkflowLlm):
        planner_calls = 0

        async def complete(self, **kwargs):
            if "投研任务 Planner" in kwargs["system_prompt"]:
                self.planner_calls += 1
                raise LlmProviderError(
                    code="llm_timeout",
                    message="raw upstream timeout detail",
                )
            return await super().complete(**kwargs)

    llm = PlannerTimeoutLlm()
    result = run_question_answer_graph(
        business_intent_catalog=business_intent_catalog,
        session_id="session-timeout",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="春光集团怎么样",
        skill_agent_node=lambda state: state,
        tool_registry_factory=lambda: ToolRegistry().snapshot(),
        llm_client=llm,
    )

    assert llm.planner_calls == 3
    assert result["errors"][-1] == {
        "node": "planner_agent",
        "message": "模型响应超时，请稍后重试。",
        "code": "llm_timeout",
        "http_status": 504,
        "handled": True,
        "retryable": True,
    }
    assert result["final_response"]["content"] == "模型响应超时，请稍后重试。"
    assert result["final_response"]["response_type"] == "execution_error_response"
    assert result["final_response"]["business_error_code"] == "llm_timeout"
    assert "raw upstream timeout detail" not in repr(result)


def test_planner_retries_transient_llm_timeout_before_succeeding(business_intent_catalog):
    class TransientPlannerTimeoutLlm(WorkflowLlm):
        planner_calls = 0

        async def complete(self, **kwargs):
            if "投研任务 Planner" in kwargs["system_prompt"]:
                self.planner_calls += 1
                if self.planner_calls == 1:
                    raise LlmProviderError(
                        code="llm_timeout",
                        message="transient upstream timeout",
                    )
            return await super().complete(**kwargs)

    llm = TransientPlannerTimeoutLlm()
    result = run_question_answer_graph(
        business_intent_catalog=business_intent_catalog,
        session_id="session-transient-timeout",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="春光集团怎么样",
        skill_agent_node=lambda state: state,
        tool_agent_node=lambda state: {
            "task_results": [
                {
                    "task_id": state["active_task"]["id"],
                    "status": "success",
                    "content": "当前没有外部经营数据，无法判断经营质量。",
                }
            ]
        },
        allow_test_tool_agent_override=True,
        tool_registry_factory=lambda: ToolRegistry().snapshot(),
        llm_client=llm,
    )

    assert llm.planner_calls == 2
    assert result["errors"] == []
    assert result["final_response"]["response_type"] == "research_answer"


def test_tool_agent_overrides_global_retry_to_avoid_replaying_side_effects(
    business_intent_catalog,
):
    class ToolTimeoutError(RuntimeError):
        code = "mcp_tool_timeout"
        http_status = 503

    calls = 0

    def timed_out_tool_agent(_state):
        nonlocal calls
        calls += 1
        raise ToolTimeoutError("upstream result is unknown")

    result = run_question_answer_graph(
        business_intent_catalog=business_intent_catalog,
        session_id="session-tool-timeout",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析海能达的经营质量",
        skill_agent_node=lambda state: state,
        tool_agent_node=timed_out_tool_agent,
        allow_test_tool_agent_override=True,
        tool_registry_factory=lambda: ToolRegistry().snapshot(),
        llm_client=WorkflowLlm(),
    )

    assert calls == 1
    assert result["final_response"]["response_type"] == "execution_error_response"
    assert result["final_response"]["business_error_code"] == "mcp_tool_timeout"


def test_full_graph_discovers_and_executes_dynamic_tool_task(
    business_intent_catalog,
):
    registry, search_service = _market_tool_runtime()

    result = run_question_answer_graph(
        business_intent_catalog=business_intent_catalog,
        session_id="session-tool",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="查询海能达最新行情并解读涨跌",
        skill_agent_node=lambda state: state,
        tool_registry_factory=lambda: registry,
        tool_search_service=search_service,
        llm_client=ToolWorkflowLlm(),
    )

    assert result["task_results"][0]["status"] == "success"
    assert any(isinstance(message, ToolMessage) for message in result["messages"])
    assert "## 行情解读" in result["final_response"]["content"]
    assert "不构成投资建议" in result["final_response"]["content"]


class FeedbackWorkflowLlm(ToolWorkflowLlm):
    def __init__(self, *, parallel=False, revise=False):
        self.parallel = parallel
        self.revise = revise
        self.review_count = 0
        self.tool_count = 0

    def bind_tools(self, tools, *, tool_choice):
        self.tool_count += 1
        return super().bind_tools(tools, tool_choice=tool_choice)

    async def complete(self, **kwargs):
        prompt = kwargs["system_prompt"]
        payload = json.loads(kwargs["user_prompt"])
        if "投研任务 Planner" in prompt:
            tasks = [
                {
                    "id": "market",
                    "task_goal": "查询海能达最新日线行情",
                    "completion_criteria": ["得到价格"],
                }
            ]
            if self.parallel:
                tasks.append({"id": "other", "task_goal": "查询海能达历史日线行情"})
            elif not self.revise:
                tasks.append(
                    {
                        "id": "compare",
                        "task_goal": "根据行情查询海能达历史日线行情",
                        "execution_type": "tool",
                        "depends_on": ["market"],
                    }
                )
            return LlmCompletionResult(
                content=json.dumps({"tasks": tasks}), model="test", provider="test"
            )
        if "计划反馈决策器" in prompt:
            self.review_count += 1
            if self.revise and self.review_count == 1:
                tasks = payload["current_plan"]["tasks"] + [
                    {
                        "id": "verify",
                        "task_goal": "核实海能达历史日线行情",
                        "execution_type": "tool",
                        "depends_on": ["market"],
                    }
                ]
                decision = {
                    "action": "revise",
                    "reason": "需要补充历史行情",
                    "plan": {"tasks": tasks},
                }
            else:
                decision = {
                    "action": "continue"
                    if self.review_count == 1 and not self.parallel
                    else "finish",
                    "reason": "根据观察选择",
                }
            return LlmCompletionResult(content=json.dumps(decision), model="test", provider="test")
        if "投研任务执行器" in prompt:
            assert "8.12" in payload["dependencies"][0]["content"]
            return LlmCompletionResult(
                content="价格为 8.12，尚需更多数据才能判断趋势。", model="test", provider="test"
            )
        return await super().complete(**kwargs)


def _feedback_run(catalog, client):
    registry, service = _market_tool_runtime()
    return run_question_answer_graph(
        business_intent_catalog=catalog,
        session_id="feedback",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析海能达价格",
        tool_registry_factory=lambda: registry,
        tool_search_service=service,
        llm_client=client,
        skill_agent_node=lambda state: state,
    )


def test_model_continues_then_finishes_multibatch_graph(business_intent_catalog):
    client = FeedbackWorkflowLlm()
    result = _feedback_run(business_intent_catalog, client)
    assert client.review_count == 2, (
        result.get("intent"),
        result.get("errors"),
        result.get("final_response"),
    )
    assert client.tool_count == 2
    assert result["planner_decision"]["action"] == "finish"
    assert {item["task_id"] for item in result["task_results"]} == {"market", "compare"}


def test_model_revises_without_reexecuting_completed_evidence(business_intent_catalog):
    client = FeedbackWorkflowLlm(revise=True)
    result = _feedback_run(business_intent_catalog, client)
    assert client.review_count == 2, (
        result.get("intent"),
        result.get("errors"),
        result.get("final_response"),
    )
    assert client.tool_count == 2
    assert result["planner_iteration"] == 2
    assert {item["task_id"] for item in result["task_results"]} == {"market", "r1_verify"}


def test_parallel_tool_discovery_updates_merge_without_conflict(business_intent_catalog):
    client = FeedbackWorkflowLlm(parallel=True)
    result = _feedback_run(business_intent_catalog, client)
    assert client.tool_count == 2
    assert result["tool_search_count"] == 2
    assert len(result["tool_calls"]) == 2


@pytest.mark.parametrize("request_flow,expected_calls", [(False, 6), (True, 5)])
def test_evidence_goes_directly_to_synthesis_without_analysis_calls(
    business_intent_catalog, request_flow, expected_calls,
):
    """两种入口协议均不再生成分析草稿；保留双取证、最终分析与证据限制。"""
    class ReplayLlm(ToolWorkflowLlm):
        def __init__(self):
            self.calls = []
            self.reviews = 0

        def bind_tools(self, tools, *, tool_choice):
            bound = super().bind_tools(tools, tool_choice=tool_choice)
            calls = self.calls

            class Counted:
                def invoke(self, messages):
                    calls.append("tool_chooser")
                    return bound.invoke(messages)

            return Counted()

        async def complete(self, **kwargs):
            prompt = kwargs["system_prompt"]
            payload = json.loads(kwargs["user_prompt"])
            if "投研任务理解器" in prompt:
                self.calls.append("router")
                response = await super().complete(**kwargs)
                content = json.loads(response.content)
                if request_flow:
                    content["route"] = "plan"
                return LlmCompletionResult(content=json.dumps(content), model="test", provider="test")
            if "投研任务 Planner" in prompt:
                self.calls.append("planner")
                content = {"tasks": [
                    {"id": "market", "task_goal": "查询最新日线行情"},
                    {"id": "history", "task_goal": "查询历史日线行情"},
                ]}
            elif "计划反馈决策器" in prompt:
                self.calls.append("review")
                self.reviews += 1
                assert len(payload["observations"]) == 2
                content = {"action": "finish",
                           "reason": "取证已完成，保留历史材料限制"}
            elif "投研任务执行器" in prompt:
                raise AssertionError("取证后的分析应由合成层完成")
            elif "投研报告合成器" in prompt:
                self.calls.append("synthesis")
                assert kwargs["thinking"] is False
                assert len(payload["research_materials"]) == 2
                assert "answer_requirements" not in payload
                answer = "价格为8.12；不足以判断趋势。"
                return LlmCompletionResult(
                    content='{"action":"answer"}\n' + answer if request_flow else answer,
                    model="test", provider="test",
                )
            else:
                raise AssertionError(prompt)
            return LlmCompletionResult(content=json.dumps(content), model="test", provider="test")

    client = ReplayLlm()
    result = _feedback_run(business_intent_catalog, client)

    assert len(client.calls) == expected_calls, (client.calls, result.get("errors"))
    assert client.calls.count("review") == (0 if request_flow else 1)
    assert client.calls.count("tool_chooser") == 2
    if not request_flow:
        assert result["planner_decision"]["action"] == "finish"
    assert {item["task_id"] for item in result["task_results"]} == {"market", "history"}
    assert "不足以判断趋势" in result["final_response"]["content"]
    assert result["verification"]["verdict"] == "pass"


def test_default_runtime_supports_twelve_sequential_tasks(business_intent_catalog):
    class ChainLlm(FeedbackWorkflowLlm):
        async def complete(self, **kwargs):
            prompt = kwargs["system_prompt"]
            data = json.loads(kwargs["user_prompt"])
            if "投研任务 Planner" in prompt:
                content = {
                    "tasks": [
                        {
                            "id": f"task_{i}",
                            "task_goal": f"根据前序证券标识查询第{i + 1}个期间的日线行情",
                            "execution_type": "tool",
                            "depends_on": [] if i == 0 else [f"task_{i - 1}"],
                        }
                        for i in range(12)
                    ]
                }
            elif "计划反馈决策器" in prompt:
                content = {
                    "action": "finish" if len(data["observations"]) == 12 else "continue",
                    "reason": "根据任务进度",
                }
            elif "投研任务执行器" in prompt:
                return LlmCompletionResult(
                    content="缺少外部材料，不能核实事实。", model="test", provider="test"
                )
            elif "投研报告合成器" in prompt:
                return LlmCompletionResult(content="材料不足。", model="test", provider="test")
            else:
                return await super().complete(**kwargs)
            return LlmCompletionResult(content=json.dumps(content), model="test", provider="test")

    result = _feedback_run(business_intent_catalog, ChainLlm())
    assert len(result["task_results"]) == 12
    assert result["planner_decision"]["action"] == "finish"
