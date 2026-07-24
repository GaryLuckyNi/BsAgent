import json
import re

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.graph.nodes.skill_agent import build_skill_agent_node
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.checkpoints import build_graph_checkpointer
from app.graph.runtime import (
    AgentGraphExecutionError,
    build_question_answer_graph,
    resume_question_answer_graph,
    run_question_answer_graph,
)
from app.graph.state import create_initial_state
from app.harness.tracing import InMemoryTraceStore
from app.llm.schemas import LlmCompletionResult
from app.schemas.evidence import EvidenceItem, EvidencePackage
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult
from app.skills.configured import ConfiguredSkill
from app.skills.definitions import SkillDefinition, SkillExecutionGuide
from app.skills.registry import SkillRegistry
from app.tools.base import Tool
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.registry import ToolRegistry


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


def test_question_answer_graph_writes_checkpoint_by_run_id():
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
        {"configurable": {"thread_id": result["request_id"]}}
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
        permission_scopes=["mcp:mock"],
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


def test_question_answer_graph_resumes_high_risk_rag_after_approval():
    trace_store = InMemoryTraceStore()
    checkpointer = InMemorySaver()

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

    retrieval_calls = []

    def fake_retriever(state):
        retrieval_calls.append(state["request_id"])
        state["evidence"] = EvidencePackage(
            evidence_package_id="evp_approved",
            query_set=[state["user_message"]],
            coverage="sufficient",
            items=[
                EvidenceItem(
                    evidence_id="ev_approved",
                    chunk_id="chunk_approved",
                    document_id="doc_approved",
                    source_uri="kb://approved",
                    title="审批后证据",
                    content="审批通过后才允许执行检索。",
                    score=0.99,
                )
            ],
        )
        return state

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        mock_evidence=False,
        skill_agent_node=fake_skill_agent,
        retriever_node=fake_retriever,
        trace_store=trace_store,
        checkpointer=checkpointer,
        permission_scopes=["rag:retrieve"],
    )

    node_names = [event.node_name for event in trace_store.events]
    interrupt_payload = result["__interrupt__"][0].value

    assert interrupt_payload["approval_type"] == "tool_call"
    assert interrupt_payload["tool_name"] == "rag_search"
    assert interrupt_payload["checkpoint_thread_id"] == result["request_id"]
    assert retrieval_calls == []
    assert "retriever_agent" not in node_names

    resumed = resume_question_answer_graph(
        checkpoint_thread_id=result["request_id"],
        approval_decision={
            "approval_id": interrupt_payload["approval_id"],
            "arguments_hash": interrupt_payload["arguments_hash"],
            "decision": "approved",
        },
        retriever_node=fake_retriever,
        skill_agent_node=fake_skill_agent,
        trace_store=trace_store,
        checkpointer=checkpointer,
        permission_scopes=["rag:retrieve"],
    )

    assert retrieval_calls == [result["request_id"]]
    assert resumed["approval_requests"][0].status == "approved"
    assert resumed["final_response"]["approval_status"] == "approved"
    assert resumed["final_response"] is not None

    rejected_checkpointer = InMemorySaver()
    rejected_initial = run_question_answer_graph(
        session_id="s2",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        mock_evidence=False,
        skill_agent_node=fake_skill_agent,
        retriever_node=fake_retriever,
        checkpointer=rejected_checkpointer,
        permission_scopes=["rag:retrieve"],
    )
    rejected_payload = rejected_initial["__interrupt__"][0].value
    rejected = resume_question_answer_graph(
        checkpoint_thread_id=rejected_initial["request_id"],
        approval_decision={
            "approval_id": rejected_payload["approval_id"],
            "arguments_hash": rejected_payload["arguments_hash"],
            "decision": "rejected",
        },
        retriever_node=fake_retriever,
        skill_agent_node=fake_skill_agent,
        checkpointer=rejected_checkpointer,
        permission_scopes=["rag:retrieve"],
    )

    assert retrieval_calls == [result["request_id"]]
    assert rejected["final_response"]["approval_status"] == "rejected"
    assert "未执行任何外部操作" in rejected["final_response"]["content"]


def test_question_answer_graph_rejects_old_hash_after_tool_arguments_change():
    checkpointer = InMemorySaver()

    def fake_skill_agent(state):
        state["plan"] = state["plan"] | {
            "requested_tools": [
                ToolCallRequest(
                    tool_name="rag_search",
                    tool_version="1.0.0",
                    arguments={"query": "原始查询"},
                    purpose="高风险检索",
                    approval_required=True,
                )
            ]
        }
        return state

    graph = build_question_answer_graph(
        retriever_node=lambda state: state,
        skill_agent_node=fake_skill_agent,
        checkpointer=checkpointer,
    )
    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="执行高风险检索",
        permission_scopes=["rag:retrieve"],
    )
    config = {"configurable": {"thread_id": state["request_id"]}}

    initial = graph.invoke(state, config=config)
    old_approval = initial["__interrupt__"][0].value
    changed_plan = {
        **initial["plan"],
        "requested_tools": [
            ToolCallRequest(
                tool_name="rag_search",
                tool_version="1.0.0",
                arguments={"query": "审批后被修改的查询"},
                purpose="高风险检索",
                approval_required=True,
            )
        ],
    }
    graph.update_state(config, {"plan": changed_plan}, as_node="skill_agent")
    changed = graph.invoke(None, config=config)
    changed_approval = changed["__interrupt__"][0].value

    assert changed_approval["arguments_hash"] != old_approval["arguments_hash"]
    with pytest.raises(AgentGraphExecutionError) as exc_info:
        graph.invoke(
            Command(
                resume={
                    "approval_id": old_approval["approval_id"],
                    "arguments_hash": old_approval["arguments_hash"],
                    "decision": "approved",
                }
            ),
            config=config,
        )

    assert exc_info.value.code == "approval_payload_mismatch"
    snapshot = graph.get_state(config)
    assert old_approval["arguments_hash"] not in snapshot.values[
        "approved_tool_call_hashes"
    ]


def test_registered_high_risk_tool_is_normalized_into_approval_gate():
    class WriteInput(BaseModel):
        value: str

    execution_calls = []

    class HighRiskWriteTool(Tool[WriteInput]):
        name = "mcp.mock.write_value"
        version = "1.0.0"
        input_model = WriteInput
        risk_level = "high"
        permission_scope = ["mcp:mock"]

        async def run(self, input_data: WriteInput) -> ToolCallResult:
            execution_calls.append(input_data.value)
            return ToolCallResult(
                tool_call_id="tool_write",
                tool_name=self.name,
                status="success",
                result={"value": input_data.value},
                trace_id="trace_tool",
            )

    def registry_factory():
        registry = ToolRegistry()
        registry.register(HighRiskWriteTool())
        return registry

    def medium_risk_skill(state):
        state["selected_skill"] = {
            "skill_name": "medium_skill",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": ["mcp.mock.write_value"],
        }
        state["plan"] = state["plan"] | {
            "tool_policy": {"allowed_tools": ["mcp.mock.write_value"]},
            "requested_tools": [
                ToolCallRequest(
                    tool_name="mcp.mock.write_value",
                    tool_version="1.0.0",
                    arguments={"value": "approved-only"},
                    purpose="验证注册端高风险升级",
                    approval_required=False,
                )
            ],
        }
        return state

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        skill_agent_node=medium_risk_skill,
        tool_agent_node=build_tool_agent_node(registry_factory=registry_factory),
        checkpointer=InMemorySaver(),
        permission_scopes=["mcp:mock"],
    )

    interrupt_payload = result["__interrupt__"][0].value
    normalized_request = result["plan"]["requested_tools"][0]
    assert normalized_request.approval_required is True
    assert interrupt_payload["tool_name"] == "mcp.mock.write_value"
    assert execution_calls == []
    assert result["tool_calls"] == []


def test_multi_tool_placeholder_is_resolved_before_high_risk_approval():
    class LookupInput(BaseModel):
        query: str

    class SubmitInput(BaseModel):
        entity_id: str

    lookup_calls = []
    submit_calls = []

    class LookupEntityTool(Tool[LookupInput]):
        name = "mcp.mock.lookup_entity"
        version = "1.0.0"
        input_model = LookupInput
        permission_scope = ["mcp:mock"]

        async def run(self, input_data: LookupInput) -> ToolCallResult:
            lookup_calls.append(input_data.query)
            return ToolCallResult(
                tool_call_id="tool_lookup",
                tool_name=self.name,
                status="success",
                result={"entity_id": "ENTITY-42"},
                trace_id="trace_tool",
            )

    class SubmitEntityTool(Tool[SubmitInput]):
        name = "mcp.mock.submit_entity"
        version = "1.0.0"
        input_model = SubmitInput
        permission_scope = ["mcp:mock"]
        risk_level = "high"

        async def run(self, input_data: SubmitInput) -> ToolCallResult:
            submit_calls.append(input_data.entity_id)
            return ToolCallResult(
                tool_call_id="tool_submit",
                tool_name=self.name,
                status="success",
                result={"submitted": input_data.entity_id},
                trace_id="trace_tool",
            )

    def registry_factory():
        registry = ToolRegistry()
        registry.register(LookupEntityTool())
        registry.register(SubmitEntityTool())
        return registry

    def staged_skill(state):
        allowed_tools = ["mcp.mock.lookup_entity", "mcp.mock.submit_entity"]
        state["selected_skill"] = {
            "skill_name": "staged_skill",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": allowed_tools,
        }
        state["plan"] = state["plan"] | {
            "tool_policy": {"allowed_tools": allowed_tools},
            "requested_tools": [
                ToolCallRequest(
                    tool_name="mcp.mock.lookup_entity",
                    tool_version="1.0.0",
                    arguments={"query": "目标实体"},
                    purpose="解析实体",
                ),
                ToolCallRequest(
                    tool_name="mcp.mock.submit_entity",
                    tool_version="1.0.0",
                    arguments={"entity_id": "$resolved_entity_id"},
                    purpose="提交高风险实体操作",
                    approval_required=True,
                ),
            ],
        }
        return state

    checkpointer = InMemorySaver()
    tool_agent = build_tool_agent_node(registry_factory=registry_factory)
    initial = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        skill_agent_node=staged_skill,
        tool_agent_node=tool_agent,
        checkpointer=checkpointer,
        permission_scopes=["mcp:mock"],
    )

    interrupt_payload = initial["__interrupt__"][0].value
    resolved_request = initial["plan"]["requested_tools"][1]
    assert lookup_calls == ["目标实体"]
    assert submit_calls == []
    assert resolved_request.arguments == {"entity_id": "ENTITY-42"}
    assert resolved_request.approval_required is True
    assert interrupt_payload["arguments"] == {"entity_id": "ENTITY-42"}
    assert interrupt_payload["arguments_hash"] == tool_call_fingerprint(resolved_request)

    resumed = resume_question_answer_graph(
        checkpoint_thread_id=initial["request_id"],
        approval_decision={
            "approval_id": interrupt_payload["approval_id"],
            "arguments_hash": interrupt_payload["arguments_hash"],
            "decision": "approved",
        },
        skill_agent_node=staged_skill,
        tool_agent_node=tool_agent,
        checkpointer=checkpointer,
        permission_scopes=["mcp:mock"],
    )

    assert lookup_calls == ["目标实体"]
    assert submit_calls == ["ENTITY-42"]
    assert [record.result.status for record in resumed["tool_calls"]] == [
        "success",
        "success",
    ]


def test_approval_arguments_use_schema_and_normalized_key_redaction():
    class NestedInput(BaseModel):
        internal_note: str = Field(json_schema_extra={"writeOnly": True})
        visible_nested: str

    class SecretInput(BaseModel):
        approval_code: str = Field(json_schema_extra={"writeOnly": True})
        signing_material: str = Field(json_schema_extra={"x-sensitive": True})
        nested: NestedInput
        visible: str

    class SecretTool(Tool[SecretInput]):
        name = "mcp.mock.secret_write"
        version = "1.0.0"
        input_model = SecretInput
        risk_level = "high"

        async def run(self, input_data: SecretInput) -> ToolCallResult:
            raise AssertionError("high-risk provider must not run before approval")

    def registry_factory():
        registry = ToolRegistry()
        registry.register(SecretTool())
        return registry

    raw_arguments = {
        "approval_code": "write-only-value",
        "signing_material": "schema-sensitive-value",
        "nested": {
            "internal_note": "schema-nested-value",
            "visible_nested": "safe-nested",
        },
        "visible": "safe-value",
    }

    def secret_skill(state):
        state["selected_skill"] = {
            "skill_name": "secret_skill",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": ["mcp.mock.secret_write"],
        }
        state["plan"] = state["plan"] | {
            "tool_policy": {"allowed_tools": ["mcp.mock.secret_write"]},
            "requested_tools": [
                ToolCallRequest.model_construct(
                    tool_name="mcp.mock.secret_write",
                    tool_version="1.0.0",
                    arguments=raw_arguments,
                    purpose="验证审批参数脱敏",
                    safe_arguments={"visible": "forged-approval-display"},
                )
            ],
        }
        return state

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        skill_agent_node=secret_skill,
        tool_agent_node=build_tool_agent_node(registry_factory=registry_factory),
        checkpointer=InMemorySaver(),
    )

    interrupt_payload = result["__interrupt__"][0].value
    staged_request = result["plan"]["requested_tools"][0]
    assert interrupt_payload["arguments"] == {
        "approval_code": "***",
        "signing_material": "***",
        "nested": {
            "internal_note": "***",
            "visible_nested": "safe-nested",
        },
        "visible": "safe-value",
    }
    assert interrupt_payload["arguments_hash"] == tool_call_fingerprint(staged_request)


def test_consecutive_high_risk_steps_are_approved_one_at_a_time():
    class FirstInput(BaseModel):
        value: str

    class SecondInput(BaseModel):
        resource_id: str

    first_calls = []
    second_calls = []

    class FirstWriteTool(Tool[FirstInput]):
        name = "mcp.mock.first_write"
        version = "1.0.0"
        input_model = FirstInput
        risk_level = "high"

        async def run(self, input_data: FirstInput) -> ToolCallResult:
            first_calls.append(input_data.value)
            return ToolCallResult(
                tool_call_id="provider_first",
                tool_name=self.name,
                status="success",
                result={"resource_id": "RESOURCE-9"},
                trace_id="trace_tool",
            )

    class SecondWriteTool(Tool[SecondInput]):
        name = "mcp.mock.second_write"
        version = "1.0.0"
        input_model = SecondInput
        risk_level = "high"

        async def run(self, input_data: SecondInput) -> ToolCallResult:
            second_calls.append(input_data.resource_id)
            return ToolCallResult(
                tool_call_id="provider_second",
                tool_name=self.name,
                status="success",
                result={"done": True},
                trace_id="trace_tool",
            )

    def registry_factory():
        registry = ToolRegistry()
        registry.register(FirstWriteTool())
        registry.register(SecondWriteTool())
        return registry

    def skill(state):
        allowed = ["mcp.mock.first_write", "mcp.mock.second_write"]
        state["selected_skill"] = {
            "skill_name": "two_writes",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": allowed,
        }
        state["plan"] = state["plan"] | {
            "tool_policy": {"allowed_tools": allowed},
            "requested_tools": [
                ToolCallRequest(
                    tool_name="mcp.mock.first_write",
                    arguments={"value": "first"},
                    purpose="第一步写入",
                ),
                ToolCallRequest(
                    tool_name="mcp.mock.second_write",
                    arguments={"resource_id": "$resolved_resource_id"},
                    purpose="第二步写入",
                ),
            ],
        }
        return state

    checkpointer = InMemorySaver()
    tool_agent = build_tool_agent_node(registry_factory=registry_factory)
    initial = run_question_answer_graph(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
        skill_agent_node=skill,
        tool_agent_node=tool_agent,
        checkpointer=checkpointer,
    )
    first_approval = initial["__interrupt__"][0].value

    assert first_approval["tool_name"] == "mcp.mock.first_write"
    assert first_calls == []
    assert second_calls == []

    after_first = resume_question_answer_graph(
        checkpoint_thread_id=initial["request_id"],
        approval_decision={
            "approval_id": first_approval["approval_id"],
            "arguments_hash": first_approval["arguments_hash"],
            "decision": "approved",
        },
        skill_agent_node=skill,
        tool_agent_node=tool_agent,
        checkpointer=checkpointer,
    )
    second_approval = after_first["__interrupt__"][0].value

    assert first_calls == ["first"]
    assert second_calls == []
    assert second_approval["tool_name"] == "mcp.mock.second_write"
    assert second_approval["arguments"] == {"resource_id": "RESOURCE-9"}
    assert second_approval["call_id"] != first_approval["call_id"]

    completed = resume_question_answer_graph(
        checkpoint_thread_id=initial["request_id"],
        approval_decision={
            "approval_id": second_approval["approval_id"],
            "arguments_hash": second_approval["arguments_hash"],
            "decision": "approved",
        },
        skill_agent_node=skill,
        tool_agent_node=tool_agent,
        checkpointer=checkpointer,
    )

    assert completed["final_response"] is not None
    assert first_calls == ["first"]
    assert second_calls == ["RESOURCE-9"]


def test_identical_tool_steps_execute_as_distinct_call_instances():
    class WriteInput(BaseModel):
        value: str

    calls = []

    class WriteTool(Tool[WriteInput]):
        name = "mcp.mock.repeat_write"
        version = "1.0.0"
        input_model = WriteInput

        async def run(self, input_data: WriteInput) -> ToolCallResult:
            calls.append(input_data.value)
            return ToolCallResult(
                tool_call_id=f"provider_{len(calls)}",
                tool_name=self.name,
                status="success",
                result={"value": input_data.value},
                trace_id="trace_tool",
            )

    def registry_factory():
        registry = ToolRegistry()
        registry.register(WriteTool())
        return registry

    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="执行两次",
    )
    state["plan"] = {
        "tool_policy": {"allowed_tools": ["mcp.mock.repeat_write"]},
        "requested_tools": [
            ToolCallRequest(
                tool_name="mcp.mock.repeat_write",
                arguments={"value": "same"},
                purpose="第一次",
            ),
            ToolCallRequest(
                tool_name="mcp.mock.repeat_write",
                arguments={"value": "same"},
                purpose="第二次",
            ),
        ],
    }

    result = build_tool_agent_node(registry_factory=registry_factory)(state)

    assert calls == ["same", "same"]
    assert len(result["tool_calls"]) == 2
    assert result["tool_calls"][0].call_id != result["tool_calls"][1].call_id


def test_identical_high_risk_steps_receive_distinct_approvals_and_execute_twice():
    class WriteInput(BaseModel):
        value: str

    calls = []

    class HighRiskWriteTool(Tool[WriteInput]):
        name = "mcp.mock.repeat_high_write"
        version = "1.0.0"
        input_model = WriteInput
        risk_level = "high"

        async def run(self, input_data: WriteInput) -> ToolCallResult:
            calls.append(input_data.value)
            return ToolCallResult(
                tool_call_id=f"provider_{len(calls)}",
                tool_name=self.name,
                status="success",
                result={"value": input_data.value},
                trace_id="trace_tool",
            )

    def registry_factory():
        registry = ToolRegistry()
        registry.register(HighRiskWriteTool())
        return registry

    def skill(state):
        allowed = ["mcp.mock.repeat_high_write"]
        state["selected_skill"] = {
            "skill_name": "repeat_write",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": allowed,
        }
        state["plan"] = state["plan"] | {
            "tool_policy": {"allowed_tools": allowed},
            "requested_tools": [
                ToolCallRequest(
                    tool_name="mcp.mock.repeat_high_write",
                    arguments={"value": "same"},
                    purpose="第一次写入",
                ),
                ToolCallRequest(
                    tool_name="mcp.mock.repeat_high_write",
                    arguments={"value": "same"},
                    purpose="第二次写入",
                ),
            ],
        }
        return state

    checkpointer = InMemorySaver()
    tool_agent = build_tool_agent_node(registry_factory=registry_factory)
    initial = run_question_answer_graph(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
        skill_agent_node=skill,
        tool_agent_node=tool_agent,
        checkpointer=checkpointer,
    )
    first = initial["__interrupt__"][0].value
    after_first = resume_question_answer_graph(
        checkpoint_thread_id=initial["request_id"],
        approval_decision={
            "approval_id": first["approval_id"],
            "arguments_hash": first["arguments_hash"],
            "decision": "approved",
        },
        skill_agent_node=skill,
        tool_agent_node=tool_agent,
        checkpointer=checkpointer,
    )
    second = after_first["__interrupt__"][0].value

    assert first["arguments_hash"] == second["arguments_hash"]
    assert first["call_id"] != second["call_id"]
    assert first["approval_id"] != second["approval_id"]
    assert calls == ["same"]

    completed = resume_question_answer_graph(
        checkpoint_thread_id=initial["request_id"],
        approval_decision={
            "approval_id": second["approval_id"],
            "arguments_hash": second["arguments_hash"],
            "decision": "approved",
        },
        skill_agent_node=skill,
        tool_agent_node=tool_agent,
        checkpointer=checkpointer,
    )

    assert completed["final_response"] is not None
    assert calls == ["same", "same"]


def test_failed_prerequisite_does_not_create_later_approval():
    class LookupInput(BaseModel):
        value: str

    class WriteInput(BaseModel):
        resource_id: str

    write_calls = []

    class FailingLookupTool(Tool[LookupInput]):
        name = "mcp.mock.failing_lookup"
        version = "1.0.0"
        input_model = LookupInput

        async def run(self, input_data: LookupInput) -> ToolCallResult:
            return ToolCallResult(
                tool_call_id="provider_lookup",
                tool_name=self.name,
                status="failed",
                error={"code": "lookup_failed", "message": "lookup failed"},
                trace_id="trace_tool",
            )

    class HighRiskWriteTool(Tool[WriteInput]):
        name = "mcp.mock.write_after_lookup"
        version = "1.0.0"
        input_model = WriteInput
        risk_level = "high"

        async def run(self, input_data: WriteInput) -> ToolCallResult:
            write_calls.append(input_data.resource_id)
            raise AssertionError("后续高风险工具不应执行")

    def registry_factory():
        registry = ToolRegistry()
        registry.register(FailingLookupTool())
        registry.register(HighRiskWriteTool())
        return registry

    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="先查再写",
    )
    allowed = ["mcp.mock.failing_lookup", "mcp.mock.write_after_lookup"]
    state["plan"] = {
        "tool_policy": {"allowed_tools": allowed},
        "requested_tools": [
            ToolCallRequest(
                tool_name="mcp.mock.failing_lookup",
                arguments={"value": "target"},
                purpose="查询前置资源",
            ),
            ToolCallRequest(
                tool_name="mcp.mock.write_after_lookup",
                arguments={"resource_id": "$resolved_resource_id"},
                purpose="写入资源",
            ),
        ],
    }

    result = build_tool_agent_node(registry_factory=registry_factory)(state)

    assert result["tool_calls"][0].result.status == "failed"
    assert result["pending_tool_approval"] is None
    assert write_calls == []


def test_high_risk_rag_request_is_prepared_by_tool_agent_before_retrieval():
    class SearchInput(BaseModel):
        query: str

    provider_calls = []
    retriever_calls = []

    class HighRiskRagTool(Tool[SearchInput]):
        name = "rag_search"
        version = "1.0.0"
        input_model = SearchInput
        risk_level = "high"

        async def run(self, input_data: SearchInput) -> ToolCallResult:
            provider_calls.append(input_data.query)
            raise AssertionError("rag_search 应由 Retriever 执行")

    def registry_factory():
        registry = ToolRegistry()
        registry.register(HighRiskRagTool())
        return registry

    def skill(state):
        state["selected_skill"] = {
            "skill_name": "high_risk_search",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": ["rag_search"],
        }
        state["plan"] = state["plan"] | {
            "tool_policy": {"allowed_tools": ["rag_search"]},
            "requested_tools": [
                ToolCallRequest(
                    tool_name="rag_search",
                    tool_version="1.0.0",
                    arguments={"query": "敏感投研资料"},
                    purpose="检索投研资料",
                )
            ],
        }
        return state

    def retriever(state):
        retriever_calls.append(state["plan"]["requested_tools"][0].call_id)
        state["evidence"] = EvidencePackage(
            evidence_package_id="evp_rag",
            query_set=["敏感投研资料"],
            coverage="none",
            items=[],
        )
        return state

    checkpointer = InMemorySaver()
    tool_agent = build_tool_agent_node(registry_factory=registry_factory)
    initial = run_question_answer_graph(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
        skill_agent_node=skill,
        tool_agent_node=tool_agent,
        retriever_node=retriever,
        checkpointer=checkpointer,
    )

    approval = initial["__interrupt__"][0].value
    assert approval["tool_name"] == "rag_search"
    assert approval["call_id"]
    assert retriever_calls == []
    assert provider_calls == []

    completed = resume_question_answer_graph(
        checkpoint_thread_id=initial["request_id"],
        approval_decision={
            "approval_id": approval["approval_id"],
            "arguments_hash": approval["arguments_hash"],
            "decision": "approved",
        },
        skill_agent_node=skill,
        tool_agent_node=tool_agent,
        retriever_node=retriever,
        checkpointer=checkpointer,
    )

    assert completed["final_response"] is not None
    assert retriever_calls == [approval["call_id"]]
    assert provider_calls == []


def test_pending_descriptor_resumes_from_sqlite_checkpoint(tmp_path):
    build_graph_checkpointer.cache_clear()
    checkpointer = build_graph_checkpointer(
        backend="sqlite",
        sqlite_path=str(tmp_path / "approval-checkpoints.sqlite"),
    )
    calls = []

    def skill(state):
        state["selected_skill"] = {
            "skill_name": "sqlite_resume",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": ["rag_search"],
        }
        state["plan"] = state["plan"] | {
            "tool_policy": {"allowed_tools": ["rag_search"]},
            "requested_tools": [
                ToolCallRequest(
                    tool_name="rag_search",
                    arguments={"query": "审批恢复"},
                    purpose="验证 SQLite 恢复",
                    approval_required=True,
                )
            ],
        }
        return state

    def retriever(state):
        calls.append(state["plan"]["requested_tools"][0].call_id)
        state["evidence"] = EvidencePackage(
            evidence_package_id="evp_sqlite",
            query_set=["审批恢复"],
            coverage="none",
            items=[],
        )
        return state

    initial = run_question_answer_graph(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
        skill_agent_node=skill,
        retriever_node=retriever,
        checkpointer=checkpointer,
    )
    approval = initial["__interrupt__"][0].value

    completed = resume_question_answer_graph(
        checkpoint_thread_id=initial["request_id"],
        approval_decision={
            "approval_id": approval["approval_id"],
            "arguments_hash": approval["arguments_hash"],
            "decision": "approved",
        },
        skill_agent_node=skill,
        retriever_node=retriever,
        checkpointer=checkpointer,
    )

    assert completed["final_response"] is not None
    assert calls == [approval["call_id"]]
    assert completed["pending_tool_approval"] is None
    assert completed["approved_tool_call_ids"] == [approval["call_id"]]
    build_graph_checkpointer.cache_clear()


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
        permission_scopes=["mcp:mock"],
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
        permission_scopes=["mcp:mock"],
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
