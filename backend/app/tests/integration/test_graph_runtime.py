import json
import re

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.graph.nodes.skill_agent import build_skill_agent_node
from app.graph.runtime import AgentGraphExecutionError, run_question_answer_graph
from app.harness.tracing import InMemoryTraceStore
from app.llm.schemas import LlmCompletionResult
from app.schemas.evidence import EvidenceItem, EvidencePackage
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult
from app.skills.configured import ConfiguredSkill
from app.skills.definitions import SkillDefinition, SkillExecutionGuide
from app.skills.registry import SkillRegistry


class FakeLlmClient:
    async def complete(self, **_):
        return LlmCompletionResult(
            content="LLM 渲染：差旅报销需要提交发票和审批单。",
            model="fake-model",
            provider="fake",
        )


class ToolRoutingLlmClient:
    def __init__(self) -> None:
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        system_prompt = kwargs.get("system_prompt", "")
        if "工具参数规划器" in system_prompt:
            payload = json.loads(kwargs["user_prompt"])
            numbers = _numbers_from_message(payload["user_message"])
            return LlmCompletionResult(
                content=json.dumps(
                    {
                        "tool_name": "mcp.mock.calculate_sum",
                        "tool_version": "mcp-stdio-v1",
                        "arguments": {"numbers": numbers},
                        "purpose": "计算用户给出的数字总和",
                        "approval_required": False,
                    },
                    ensure_ascii=False,
                ),
                model="fake-planner-model",
                provider="fake",
            )
        if "工具结果回答渲染器" in system_prompt:
            payload = json.loads(kwargs["user_prompt"])
            total = payload["tool_result"]["total"]
            return LlmCompletionResult(
                content=f"计算结果是 {total}。",
                model="fake-render-model",
                provider="fake",
            )
        return LlmCompletionResult(
            content="""
            {
              "intent": "tool_action",
              "domain": "mcp_math",
              "sub_intent": "calculate_sum",
              "candidate_skill": null,
              "candidate_tool": "mcp.mock.calculate_sum",
              "risk_level": "low",
              "confidence": 0.91,
              "needs_clarification": false,
              "reason_codes": ["classifier_selected_sum_tool"]
            }
            """,
            model="fake-intent-model",
            provider="fake",
        )


class ConversationRoutingLlmClient:
    async def complete(self, **kwargs):
        system_prompt = kwargs.get("system_prompt", "")
        if "最终回答渲染器" in system_prompt:
            return LlmCompletionResult(
                content="我已记下你的偏好，后续文档会优先使用中文。",
                model="fake-render-model",
                provider="fake",
            )
        return LlmCompletionResult(
            content="""
            {
              "intent": "conversation",
              "domain": "preference_ack",
              "sub_intent": "remember_user_preference",
              "candidate_skill": null,
              "candidate_tool": null,
              "risk_level": "low",
              "confidence": 0.88,
              "needs_clarification": false,
              "reason_codes": ["classifier_selected_preference_ack"]
            }
            """,
            model="fake-intent-model",
            provider="fake",
        )


def _numbers_from_message(message: str) -> list[int]:
    range_match = re.search(r"(\d+)\s*(?:加到|到|-|~)\s*(\d+)", message)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        step = 1 if end >= start else -1
        return list(range(start, end + step, step))
    return [int(value) for value in re.findall(r"\d+", message)]


def _a_share_company_snapshot_skill_registry() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(
        ConfiguredSkill(
            definition=SkillDefinition(
                name="company_snapshot",
                version="1.0.0",
                description="生成 A 股公司速览",
                input_contract={
                    "supported_intents": ["task_workflow"],
                    "supported_domains": ["a_share_equity_research"],
                    "supported_sub_intents": ["company_snapshot"],
                },
                output_contract={},
                allowed_tools=[
                    "mcp.mock.stock.resolve_company",
                    "mcp.mock.stock.company_profile",
                    "mcp.mock.stock.market_snapshot",
                    "rag_search",
                ],
                constraints=["必须追加投研免责声明"],
                failure_modes=[],
                definition_hash="hash_definition",
            ),
            guide=SkillExecutionGuide(
                name="company_snapshot",
                version="1.0.0",
                instructions=[],
                required_evidence_policy="answer_prefers_cited_evidence",
                definition_hash="hash_guide",
            ),
            risk_level="medium",
        )
    )
    return registry


def test_question_answer_graph_returns_final_response_with_trace():
    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
        mock_evidence=True,
    )

    assert result["final_response"]["trace_id"].startswith("trace_")
    assert "差旅报销需要提交发票和审批单" in result["final_response"]["content"]
    assert result["selected_skill"]["skill_name"] == "policy_qa"
    assert result["selected_skill"]["status"] == "needs_retrieval"
    assert result["plan"]["requested_tools"][0].tool_name == "rag_search"
    assert result["verification"]["verdict"] == "pass"


def test_question_answer_graph_writes_checkpoint_by_session_id():
    checkpointer = InMemorySaver()

    result = run_question_answer_graph(
        session_id="checkpoint_s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
        mock_evidence=True,
        checkpointer=checkpointer,
    )

    checkpoint = checkpointer.get_tuple(
        {"configurable": {"thread_id": result["session_id"]}}
    )

    assert checkpoint is not None
    assert checkpoint.checkpoint["channel_values"]["final_response"]["trace_id"] == result[
        "trace_id"
    ]


def test_question_answer_graph_stops_when_node_records_error():
    def failing_retriever(state):
        raise RuntimeError("rag unavailable")

    with pytest.raises(AgentGraphExecutionError) as exc_info:
        run_question_answer_graph(
            session_id="s1",
            tenant_id="t1",
            user_id="u1",
            user_message="差旅报销需要什么？",
            mock_evidence=True,
            retriever_node=failing_retriever,
        )

    assert exc_info.value.node_name == "retriever_agent"
    assert "rag unavailable" in str(exc_info.value)


def test_question_answer_graph_preserves_structured_node_error_metadata():
    class ControlledError(RuntimeError):
        code = "rag_index_unavailable"
        http_status = 503

    def failing_retriever(_):
        raise ControlledError("No Chroma index found.")

    with pytest.raises(AgentGraphExecutionError) as exc_info:
        run_question_answer_graph(
            session_id="s1",
            tenant_id="t1",
            user_id="u1",
            user_message="差旅报销需要什么？",
            mock_evidence=True,
            retriever_node=failing_retriever,
        )

    assert exc_info.value.code == "rag_index_unavailable"
    assert exc_info.value.http_status == 503


def test_question_answer_graph_can_use_injected_retriever_without_mock_evidence():
    def injected_retriever(state):
        state["evidence"] = EvidencePackage.model_validate(
            {
                "evidence_package_id": "evp_injected",
                "query_set": [state["user_message"]],
                "coverage": "sufficient",
                "items": [
                    {
                        "evidence_id": "ev_1",
                        "chunk_id": "chunk_1",
                        "document_id": "doc_1",
                        "title": "差旅制度",
                        "content": "差旅报销需要提交发票和审批单。",
                        "score": 0.95,
                    }
                ],
            }
        )
        return state

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
        mock_evidence=False,
        retriever_node=injected_retriever,
    )

    assert result["verification"]["verdict"] == "pass"


def test_question_answer_graph_runs_memory_manager_before_intent_router():
    trace_store = InMemoryTraceStore()

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="记住我的语言偏好，所有的文档用中文",
        mock_evidence=True,
        trace_store=trace_store,
        llm_client=ConversationRoutingLlmClient(),
    )

    assert result["memory_candidates"][0].memory_type == "user_preference"
    assert [event.node_name for event in trace_store.events][:2] == [
        "memory_manager",
        "intent_router",
    ]


def test_question_answer_graph_runs_planner_between_intent_and_skill():
    trace_store = InMemoryTraceStore()

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        mock_evidence=True,
        trace_store=trace_store,
    )

    node_names = [event.node_name for event in trace_store.events]

    assert node_names.index("intent_router") < node_names.index("planner_agent")
    assert node_names.index("planner_agent") < node_names.index("skill_agent")
    assert result["plan"]["objective"] == "回答用户的知识问答请求"


def test_question_answer_graph_skips_skill_and_retrieval_for_conversation_intent():
    trace_store = InMemoryTraceStore()

    def failing_retriever(_):
        raise AssertionError("conversation intent should not call retriever")

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="你好",
        retriever_node=failing_retriever,
        trace_store=trace_store,
    )

    node_names = [event.node_name for event in trace_store.events]

    assert result["intent"]["intent"] == "conversation"
    assert result["plan"]["execution_mode"] == "direct_response"
    assert result["verification"]["verdict"] == "pass"
    assert result["final_response"]["content"]
    assert "skill_agent" not in node_names
    assert "retriever_agent" not in node_names
    assert "analysis_agent" not in node_names


def test_question_answer_graph_executes_mcp_calculate_sum_tool_action():
    trace_store = InMemoryTraceStore()

    def fake_tool_agent(state):
        request = state["plan"]["requested_tools"][0]
        state["tool_calls"].append(
            ToolCallRecord(
                request=request,
                result=ToolCallResult(
                    tool_call_id="tool_1",
                    tool_name=request.tool_name,
                    status="success",
                    result={"total": 1275, "count": 50},
                    trace_id=state["trace_id"],
                ),
            )
        )
        return state

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="计算1加到50",
        tool_agent_node=fake_tool_agent,
        trace_store=trace_store,
        llm_client=ToolRoutingLlmClient(),
    )

    node_names = [event.node_name for event in trace_store.events]

    assert result["intent"]["intent"] == "tool_action"
    assert result["plan"]["execution_mode"] == "tool_execution"
    assert result["tool_calls"][0].request.tool_name == "mcp.mock.calculate_sum"
    assert result["final_response"]["approval_status"] == "not_required"
    assert "1275" in result["final_response"]["content"]
    assert "tool_agent" in node_names
    assert "skill_agent" not in node_names
    assert "retriever_agent" not in node_names


def test_question_answer_graph_executes_real_mcp_stdio_tool_action():
    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="计算1加到50",
        llm_client=ToolRoutingLlmClient(),
    )

    assert result["plan"]["execution_mode"] == "tool_execution"
    assert result["tool_calls"][0].request.tool_name == "mcp.mock.calculate_sum"
    assert result["tool_calls"][0].result.status == "success"
    assert result["tool_calls"][0].result.result == {"total": 1275, "count": 50}
    assert "1275" in result["final_response"]["content"]


def test_question_answer_graph_executes_structured_skill_tools_before_retrieval():
    trace_store = InMemoryTraceStore()

    def fake_skill_agent(state):
        state["selected_skill"] = {
            "skill_name": "company_snapshot",
            "skill_version": "1.0.0",
            "status": "needs_retrieval",
            "reason_codes": ["configured_skill_from_registry"],
        }
        state["plan"] = state["plan"] | {
            "requested_tools": [
                ToolCallRequest(
                    tool_name="mcp.mock.stock.resolve_company",
                    arguments={"query": "贵州茅台"},
                    purpose="解析公司简称",
                ),
                ToolCallRequest(
                    tool_name="rag_search",
                    tool_version="1.0.0",
                    arguments={
                        "query": state["user_message"],
                        "tenant_id": state["tenant_id"],
                        "user_id": state["user_id"],
                        "top_k": 8,
                        "retrieval_mode": "hybrid",
                    },
                    purpose="检索公司相关非结构化证据",
                ),
            ],
            "skill_output": {"skill_name": "company_snapshot"},
        }
        return state

    def fake_tool_agent(state):
        request = next(
            request
            for request in state["plan"]["requested_tools"]
            if request.tool_name != "rag_search"
        )
        state["tool_calls"].append(
            ToolCallRecord(
                request=request,
                result=ToolCallResult(
                    tool_call_id="tool_stock_1",
                    tool_name=request.tool_name,
                    status="success",
                    result={"symbol": "600519", "name": "贵州茅台"},
                    trace_id=state["trace_id"],
                ),
            )
        )
        return state

    def fake_retriever(state):
        assert any(
            record.request.tool_name == "mcp.mock.stock.resolve_company"
            for record in state["tool_calls"]
        )
        state["evidence"] = EvidencePackage(
            evidence_package_id="evp_stock",
            query_set=[state["user_message"]],
            coverage="sufficient",
            items=[
                EvidenceItem(
                    evidence_id="ev_stock_1",
                    chunk_id="chunk_stock_1",
                    document_id="doc_stock_1",
                    title="贵州茅台公告",
                    content="贵州茅台披露经营保持稳健。",
                    score=0.91,
                )
            ],
        )
        return state

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下贵州茅台",
        mock_evidence=False,
        skill_agent_node=fake_skill_agent,
        tool_agent_node=fake_tool_agent,
        retriever_node=fake_retriever,
        trace_store=trace_store,
    )

    node_names = [event.node_name for event in trace_store.events]

    assert result["tool_calls"][0].request.tool_name == "mcp.mock.stock.resolve_company"
    assert node_names.index("skill_agent") < node_names.index("tool_agent")
    assert node_names.index("tool_agent") < node_names.index("retriever_agent")
    assert result["verification"]["verdict"] == "pass"


def test_question_answer_graph_completes_a_share_company_snapshot():
    trace_store = InMemoryTraceStore()

    def fake_retriever(state):
        state["evidence"] = EvidencePackage(
            evidence_package_id="evp_1",
            query_set=["贵州茅台 年报 公司研究"],
            coverage="sufficient",
            items=[
                EvidenceItem(
                    evidence_id="ev_1",
                    chunk_id="chunk_1",
                    document_id="doc_1",
                    title="贵州茅台 2024 年报",
                    content="贵州茅台主营茅台酒及系列酒，收入结构以茅台酒为核心。",
                    score=0.92,
                )
            ],
        )
        return state

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下贵州茅台",
        mock_evidence=False,
        retriever_node=fake_retriever,
        skill_agent_node=build_skill_agent_node(
            registry=_a_share_company_snapshot_skill_registry()
        ),
        trace_store=trace_store,
    )

    content = result["final_response"]["content"]
    node_names = [event.node_name for event in trace_store.events]

    assert result["intent"]["domain"] == "a_share_equity_research"
    assert result["selected_skill"]["skill_name"] == "company_snapshot"
    assert any(
        call.request.tool_name == "mcp.mock.stock.resolve_company"
        for call in result["tool_calls"]
    )
    assert "tool_agent" in node_names
    assert "retriever_agent" in node_names
    assert "公司概览" in content
    assert "贵州茅台" in content
    assert "不构成投资建议" in content


def test_question_answer_graph_requests_approval_for_high_risk_rag_skill_before_retrieval():
    trace_store = InMemoryTraceStore()

    def fake_skill_agent(state):
        state["selected_skill"] = {
            "skill_name": "high_risk_research",
            "skill_version": "1.0.0",
            "status": "needs_retrieval",
            "reason_codes": ["configured_skill_from_registry"],
        }
        state["plan"] = state["plan"] | {
            "requested_tools": [
                ToolCallRequest(
                    tool_name="rag_search",
                    tool_version="1.0.0",
                    arguments={
                        "query": state["user_message"],
                        "tenant_id": state["tenant_id"],
                        "user_id": state["user_id"],
                        "top_k": 8,
                        "retrieval_mode": "hybrid",
                    },
                    purpose="高风险投研检索",
                    approval_required=True,
                )
            ],
            "skill_output": {"skill_name": "high_risk_research"},
        }
        return state

    def failing_retriever(_):
        raise AssertionError("approval-required rag_search should not execute")

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        mock_evidence=False,
        skill_agent_node=fake_skill_agent,
        retriever_node=failing_retriever,
        trace_store=trace_store,
    )

    node_names = [event.node_name for event in trace_store.events]

    assert result["verification"]["verdict"] == "needs_approval"
    assert result["approval_status"].status == "pending"
    assert "retriever_agent" not in node_names


def test_question_answer_graph_routes_llm_detected_sum_expression_to_tool_agent():
    trace_store = InMemoryTraceStore()
    llm_client = ToolRoutingLlmClient()

    def failing_retriever(_):
        raise AssertionError("sum expression should route to tool_agent, not retriever_agent")

    def fake_tool_agent(state):
        request = state["plan"]["requested_tools"][0]
        numbers = request.arguments["numbers"]
        state["tool_calls"].append(
            ToolCallRecord(
                request=request,
                result=ToolCallResult(
                    tool_call_id="tool_1",
                    tool_name=request.tool_name,
                    status="success",
                    result={"total": sum(numbers), "count": len(numbers)},
                    trace_id=state["trace_id"],
                ),
            )
        )
        return state

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="你可以帮我算一下1+2+3+4+5+6+7+8+9+10等于多少吗",
        retriever_node=failing_retriever,
        tool_agent_node=fake_tool_agent,
        trace_store=trace_store,
        llm_client=llm_client,
    )

    node_names = [event.node_name for event in trace_store.events]

    assert result["intent"]["intent"] == "tool_action"
    assert result["intent"]["candidate_tool"] == "mcp.mock.calculate_sum"
    assert result["plan"]["execution_mode"] == "tool_execution"
    assert result["plan"]["requested_tools"][0].arguments == {"numbers": list(range(1, 11))}
    assert result["tool_calls"][0].result.result == {"total": 55, "count": 10}
    assert "55" in result["final_response"]["content"]
    assert "tool_agent" in node_names
    assert "retriever_agent" not in node_names
    assert llm_client.calls
    assert "available_tools" in llm_client.calls[0]["user_prompt"]
    assert "mcp.mock.calculate_sum" in llm_client.calls[0]["user_prompt"]


def test_question_answer_graph_routes_short_sum_expression_to_tool_agent():
    trace_store = InMemoryTraceStore()
    llm_client = ToolRoutingLlmClient()

    def failing_retriever(_):
        raise AssertionError("short sum expression should route to tool_agent, not retriever_agent")

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="1+2+3=?",
        retriever_node=failing_retriever,
        trace_store=trace_store,
        llm_client=llm_client,
    )

    node_names = [event.node_name for event in trace_store.events]

    assert result["intent"]["intent"] == "tool_action"
    assert result["intent"]["candidate_tool"] == "mcp.mock.calculate_sum"
    assert result["plan"]["execution_mode"] == "tool_execution"
    assert result["plan"]["requested_tools"][0].arguments == {"numbers": [1, 2, 3]}
    assert result["tool_calls"][0].result.result == {"total": 6, "count": 3}
    assert "6" in result["final_response"]["content"]
    assert "tool_agent" in node_names
    assert "retriever_agent" not in node_names


def test_question_answer_graph_can_render_answer_with_llm_client():
    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        mock_evidence=True,
        llm_client=FakeLlmClient(),
    )

    assert result["final_response"]["content"] == "LLM 渲染：差旅报销需要提交发票和审批单。"
    assert result["draft_answer"]["claims"][0]["evidence_ids"] == ["ev_1"]
    assert result["verification"]["verdict"] == "pass"


def test_question_answer_graph_loads_injected_memory_retriever_context():
    def retrieve_memory(query: str, *, tenant_id: str, user_id: str) -> EvidencePackage:
        assert query == "差旅报销需要什么？"
        assert tenant_id == "tenant_1"
        assert user_id == "user_1"
        return EvidencePackage(
            evidence_package_id="evp_memory",
            query_set=[query],
            coverage="sufficient",
            items=[
                EvidenceItem(
                    evidence_id="memory_1",
                    chunk_id="experience/memcand_1",
                    document_id="experience/user_1",
                    title="用户偏好",
                    content="用户偏好：所有回答用中文",
                    score=1.0,
                    metadata={"memory_type": "user_preference"},
                )
            ],
        )

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        mock_evidence=True,
        memory_retriever=retrieve_memory,
    )

    assert result["memory_context"].entries == ["用户偏好：所有回答用中文"]
    assert result["memory_context"].sources == ["experience/memcand_1"]
