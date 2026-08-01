import json

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.graph.nodes.skill_agent import build_skill_agent_node
from app.graph.nodes.retriever_agent import build_rag_retriever_node
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.checkpoints import build_graph_checkpointer
from app.graph.runtime import (
    AgentGraphExecutionError,
    build_question_answer_graph,
    resume_question_answer_graph,
    run_question_answer_graph,
    stream_question_answer_graph,
)
from app.graph.state import create_initial_state
from app.schemas.approvals import ReadyToolCall
from app.harness.tracing import InMemoryTraceStore
from app.llm.schemas import LlmCompletionResult
from app.schemas.evidence import EvidenceItem, EvidencePackage
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult, ToolError
from app.skills.configured import ConfiguredSkill
from app.skills.definitions import SkillDefinition, SkillExecutionGuide
from app.skills.registry import SkillRegistry
from app.tools.base import Tool
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.rag_search import RagSearchTool
from app.tools.registry import ToolRegistry
from app.tools.registry import ExecutionBinding


def test_graph_routes_validated_image_context_without_rag_or_tools():
    trace_store = InMemoryTraceStore()

    class ImageOnlyLlm:
        async def complete(self, **kwargs):
            if "意图分类器" in kwargs.get("system_prompt", ""):
                return LlmCompletionResult(
                    content=json.dumps(
                        {
                            "intent": "knowledge_qa",
                            "domain": "financial_image",
                            "sub_intent": "describe_image",
                            "candidate_skill": None,
                            "candidate_tool": None,
                            "risk_level": "medium",
                            "confidence": 0.9,
                            "needs_clarification": False,
                            "reason_codes": ["image_summary_available"],
                        }
                    ),
                    model="fake-model",
                    provider="fake",
                )
            return LlmCompletionResult(
                content="营业收入同比增长。",
                model="fake-model",
                provider="fake",
            )

    class AttachmentResolver:
        def resolve(self, asset_ids, *, principal, session_id):
            assert asset_ids == ["asset-1"]
            assert principal.tenant_id == "tenant-1"
            assert session_id == "session-image"
            return [
                {
                    "asset_id": "asset-1",
                    "analysis_id": "analysis-1",
                    "media_type": "image/png",
                    "width": 100,
                    "height": 80,
                    "status": "ready",
                    "document_type": "financial_statement",
                    "summary": "营业收入同比增长。",
                    "extracted_text": "营业收入 100 亿元",
                    "financial_metrics": [],
                    "chart_observations": ["同比增长"],
                    "entities": ["营业收入"],
                    "risk_flags": ["缺少审计意见"],
                    "confidence": 0.9,
                    "truncated": False,
                }
            ]

    state = run_question_answer_graph(
        session_id="session-image",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="请描述附件内容",
        mock_evidence=True,
        trace_store=trace_store,
        attachment_ids=["asset-1"],
        attachment_resolver=AttachmentResolver(),
        llm_client=ImageOnlyLlm(),
    )

    node_names = [event.node_name for event in trace_store.list_events(state["trace_id"])]
    assert node_names.index("attachment_analyzer") < node_names.index("memory_manager")
    assert "analysis_agent" in node_names
    assert "retriever_agent" not in node_names
    assert "skill_agent" not in node_names
    assert "tool_agent" not in node_names
    assert "营业收入同比增长" in state["final_response"]["content"]


def test_graph_returns_structured_not_ready_error_from_attachment_analyzer():
    from app.assets.errors import AssetNotReadyError
    trace_store = InMemoryTraceStore()

    class NotReadyResolver:
        def resolve(self, *_args, **_kwargs):
            raise AssetNotReadyError("provider internal detail")

    result = run_question_answer_graph(
        session_id="session-image",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析这张图片",
        attachment_ids=["asset-1"],
        attachment_resolver=NotReadyResolver(),
        trace_store=trace_store,
    )

    assert result["errors"][-1]["node"] == "attachment_analyzer"
    assert result["errors"][-1]["code"] == "asset_not_ready"
    assert result["errors"][-1]["http_status"] == 409
    assert result["errors"][-1]["handled"] is True
    assert result["final_response"]["business_error_code"] == "asset_not_ready"
    assert "图片资产尚未完成分析" in result["final_response"]["content"]
    events = [
        event
        for event in trace_store.events
        if event.node_name == "attachment_analyzer"
    ]
    assert [(event.status, event.error) for event in events] == [
        ("handled_error", "asset_not_ready:AssetNotReadyError")
    ]


def test_financial_image_skill_without_tools_still_runs_analysis_agent():
    class AttachmentResolver:
        def resolve(self, *_args, **_kwargs):
            return [
                {
                    "asset_id": "asset-1",
                    "analysis_id": "analysis-1",
                    "media_type": "image/png",
                    "width": 100,
                    "height": 80,
                    "status": "ready",
                    "document_type": "financial_statement",
                    "summary": "贵州茅台营业收入增长。",
                    "extracted_text": "",
                    "financial_metrics": [],
                    "chart_observations": [],
                    "entities": ["贵州茅台", "600519"],
                    "risk_flags": [],
                    "confidence": 0.9,
                    "truncated": False,
                }
            ]

    def image_skill(state):
        state["selected_skill"] = {
            "skill_name": "financial_report_analysis",
            "skill_version": "1.0.0",
            "status": "success",
            "allowed_tools": [],
        }
        state["plan"] = {**state["plan"], "requested_tools": []}
        return state

    trace_store = InMemoryTraceStore()
    result = run_question_answer_graph(
        session_id="session-image-skill",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="结合这张财报分析公司基本面",
        attachment_ids=["asset-1"],
        attachment_resolver=AttachmentResolver(),
        skill_agent_node=image_skill,
        trace_store=trace_store,
    )

    node_names = [event.node_name for event in trace_store.events]
    assert node_names.index("skill_agent") < node_names.index("analysis_agent")
    assert result["analysis"]["findings"] == ["贵州茅台营业收入增长。"]


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
                evidence_contract={
                    "requirements": [
                        {
                            "capability": "stock.resolve_company",
                            "required_fields": ["symbol", "name"],
                            "optional_fields": ["exchange"],
                        },
                        {
                            "capability": "stock.company_profile",
                            "required_fields": ["name", "industry", "main_business"],
                            "optional_fields": ["board", "registered_address"],
                        },
                        {
                            "capability": "stock.market_snapshot",
                            "required_fields": ["price"],
                            "optional_fields": ["pe_ttm", "pb"],
                        },
                    ]
                },
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


def _rag_skill_agent(state):
    request = ToolCallRequest(
        tool_name="rag_search",
        arguments={
            "query": state["user_message"],
            "tenant_id": state["tenant_id"],
            "user_id": state["user_id"],
            "top_k": 8,
            "retrieval_mode": "hybrid",
        },
        purpose="检索投研证据",
    )
    state["selected_skill"] = {
        "skill_name": "research_evidence",
        "skill_version": "1.0.0",
        "status": "needs_retrieval",
        "reason_codes": ["test_rag_skill"],
        "allowed_tools": ["rag_search"],
    }
    state["plan"] = {
        **(state["plan"] or {}),
        "execution_mode": "skill_execution",
        "requested_tools": [request],
        "retrieval": {"required": True},
        "tool_policy": {"allowed_tools": ["rag_search"]},
        "response_policy": {
            "response_type": "knowledge_answer",
            "citation_required": True,
        },
    }
    return state


def _market_data_envelope(
    *,
    status: str,
    data: dict,
    requested_fields: list[str],
    provided_fields: list[str],
    missing_optional_fields: list[str] | None = None,
    source_apis: list[str] | None = None,
) -> dict:
    return {
        "status": status,
        "data": data,
        "coverage": {
            "status": status,
            "requested_fields": requested_fields,
            "required_fields": [],
            "optional_fields": requested_fields,
            "provided_fields": provided_fields,
            "missing_required_fields": [],
            "missing_optional_fields": missing_optional_fields or [],
            "source_apis": source_apis or [],
        },
    }


class AcceptanceMarketSnapshotInput(BaseModel):
    symbol: str
    required_fields: list[str]
    optional_fields: list[str] = Field(default_factory=list)


class AcceptanceMarketSnapshotTool(Tool[AcceptanceMarketSnapshotInput]):
    name = "mcp.production.stock.market_snapshot"
    version = "acceptance-v1"
    input_model = AcceptanceMarketSnapshotInput

    def __init__(self, *, payload: dict | None = None, error: ToolError | None = None):
        self.payload = payload
        self.error = error

    async def run(self, input_data: AcceptanceMarketSnapshotInput) -> ToolCallResult:
        del input_data
        if self.error is not None:
            return ToolCallResult(
                tool_call_id="untrusted_market_call",
                tool_name=self.name,
                status="failed",
                error=self.error,
                trace_id="untrusted_trace",
            )
        return ToolCallResult(
            tool_call_id="untrusted_market_call",
            tool_name=self.name,
            status="success",
            result=self.payload,
            trace_id="untrusted_trace",
        )


def _acceptance_market_skill_agent(
    *,
    required_fields: list[str],
    optional_fields: list[str] | None = None,
):
    def node(state):
        request = ToolCallRequest(
            tool_name="mcp.production.stock.market_snapshot",
            arguments={
                "symbol": "300085.SZ",
                "required_fields": required_fields,
                "optional_fields": optional_fields or [],
            },
            purpose="获取银之杰行情与估值字段",
        )
        state["selected_skill"] = {
            "skill_name": "company_snapshot",
            "skill_version": "1.0.0",
            "status": "success",
            "reason_codes": ["acceptance_skill"],
            "allowed_tools": [request.tool_name],
        }
        state["plan"] = {
            **(state["plan"] or {}),
            "execution_mode": "skill_execution",
            "requested_tools": [request],
            "retrieval": {"required": False},
            "tool_policy": {"allowed_tools": [request.tool_name]},
            "response_policy": {
                "response_type": "equity_research_company_snapshot",
                "citation_required": False,
                "disclaimer_required": True,
            },
        }
        return state

    return node


def _acceptance_market_registry(
    *,
    payload: dict | None = None,
    error: ToolError | None = None,
):
    def factory() -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(AcceptanceMarketSnapshotTool(payload=payload, error=error))
        return registry

    return factory


def test_question_answer_graph_writes_checkpoint_by_internal_session_key():
    checkpointer = InMemorySaver()

    result = run_question_answer_graph(
        session_id="checkpoint_s1",
        tenant_id="t1",
        user_id="u1",
        user_message="你好",
        checkpointer=checkpointer,
    )

    checkpoint = checkpointer.get_tuple(
        {"configurable": {"thread_id": result["session_key"]}}
    )

    assert checkpoint is not None
    assert checkpoint.checkpoint["channel_values"]["final_response"]["trace_id"] == result[
        "trace_id"
    ]


def test_catalog_guard_records_trace_before_rejecting_changed_catalog():
    trace_store = InMemoryTraceStore()
    graph = build_question_answer_graph(
        retriever_node=lambda state: state,
        trace_store=trace_store,
    )
    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="执行请求",
        tool_catalog_digest="stale_catalog_digest",
    )

    with pytest.raises(AgentGraphExecutionError) as exc_info:
        graph.invoke(state)

    event = trace_store.events[0]
    assert exc_info.value.code == "tool_catalog_changed"
    assert event.node_name == "catalog_guard"
    assert event.status == "error"
    assert event.error == "tool_catalog_changed:ToolCatalogChangedError"
    assert len(trace_store.events) == 1


def test_question_answer_graph_stops_when_node_records_error():
    def failing_retriever(state):
        raise RuntimeError("rag unavailable")

    with pytest.raises(AgentGraphExecutionError) as exc_info:
        run_question_answer_graph(
            session_id="s1",
            tenant_id="t1",
            user_id="u1",
            user_message="分析一下银之杰",
            mock_evidence=True,
            skill_agent_node=_rag_skill_agent,
            retriever_node=failing_retriever,
        )

    assert exc_info.value.node_name == "retriever_agent"
    assert exc_info.value.code == "agent_node_error"
    assert str(exc_info.value) == "retriever_agent: 节点执行失败。"


def test_question_answer_graph_preserves_structured_node_error_metadata():
    class ControlledError(RuntimeError):
        code = "rag_index_unavailable"
        http_status = 503

    def failing_retriever(_):
        raise ControlledError("No Chroma index found.")

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下银之杰",
        mock_evidence=True,
        skill_agent_node=_rag_skill_agent,
        retriever_node=failing_retriever,
    )

    assert result["errors"][-1]["code"] == "rag_index_unavailable"
    assert result["errors"][-1]["http_status"] == 503
    assert result["errors"][-1]["handled"] is True
    assert result["final_response"]["business_error_code"] == "rag_index_unavailable"


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
        user_message="分析一下银之杰",
        mock_evidence=False,
        skill_agent_node=_rag_skill_agent,
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
    node_names = [event.node_name for event in trace_store.events]
    assert node_names.index("attachment_analyzer") < node_names.index("memory_manager")
    assert node_names.index("memory_manager") < node_names.index("intent_router")


def test_question_answer_graph_runs_planner_between_intent_and_skill():
    trace_store = InMemoryTraceStore()

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
        mock_evidence=True,
        trace_store=trace_store,
        skill_agent_node=_rag_skill_agent,
    )

    node_names = [event.node_name for event in trace_store.events]

    assert node_names.index("intent_router") < node_names.index("planner_agent")
    assert node_names.index("planner_agent") < node_names.index("skill_agent")
    assert result["plan"]["objective"] == "规划并执行用户请求的任务流程"


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


def test_question_answer_graph_appends_messages_across_session_checkpoint():
    checkpointer = InMemorySaver()

    first = run_question_answer_graph(
        session_id="session_multi_turn",
        session_key="session-key-multi-turn",
        tenant_id="tenant_1",
        user_id="user_1",
        request_id="request-1",
        trace_id="trace-1",
        user_message_id="message-user-1",
        assistant_message_id="message-assistant-1",
        history=[],
        user_message="你好",
        checkpointer=checkpointer,
        llm_client=ConversationRoutingLlmClient(),
    )
    second = run_question_answer_graph(
        session_id="session_multi_turn",
        session_key="session-key-multi-turn",
        tenant_id="tenant_1",
        user_id="user_1",
        request_id="request-2",
        trace_id="trace-2",
        user_message_id="message-user-2",
        assistant_message_id="message-assistant-2",
        history=[],
        user_message="继续使用中文",
        checkpointer=checkpointer,
        llm_client=ConversationRoutingLlmClient(),
    )

    assert first["request_id"] != second["request_id"]
    assert second["user_message"] == "继续使用中文"
    assert [message.role for message in second["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [message.content for message in second["messages"]][::2] == [
        "你好",
        "继续使用中文",
    ]
    assert len(
        {
            message.message_id
            for message in second["messages"]
        }
    ) == 4


def test_question_answer_graph_isolates_same_external_session_by_tenant_session_key():
    checkpointer = InMemorySaver()

    first = run_question_answer_graph(
        session_id="shared-session",
        session_key="tenant-a-session-key",
        tenant_id="tenant-a",
        user_id="user-1",
        request_id="request-a",
        trace_id="trace-a",
        user_message_id="message-user-a",
        assistant_message_id="message-assistant-a",
        user_message="租户 A 的问题",
        history=[],
        checkpointer=checkpointer,
        llm_client=ConversationRoutingLlmClient(),
    )
    second = run_question_answer_graph(
        session_id="shared-session",
        session_key="tenant-b-session-key",
        tenant_id="tenant-b",
        user_id="user-1",
        request_id="request-b",
        trace_id="trace-b",
        user_message_id="message-user-b",
        assistant_message_id="message-assistant-b",
        user_message="租户 B 的问题",
        history=[],
        checkpointer=checkpointer,
        llm_client=ConversationRoutingLlmClient(),
    )

    assert [message.content for message in first["messages"]][0] == "租户 A 的问题"
    assert [message.content for message in second["messages"]][0] == "租户 B 的问题"
    assert all("租户 A" not in message.content for message in second["messages"])
    assert checkpointer.get(
        {"configurable": {"thread_id": "tenant-a-session-key"}}
    ) is not None
    assert checkpointer.get(
        {"configurable": {"thread_id": "tenant-b-session-key"}}
    ) is not None


def test_question_answer_graph_streams_node_updates_with_final_state():
    checkpointer = InMemorySaver()
    trace_store = InMemoryTraceStore()

    updates = list(
        stream_question_answer_graph(
            session_id="session_stream",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="你好",
            checkpointer=checkpointer,
            trace_store=trace_store,
            llm_client=ConversationRoutingLlmClient(),
        )
    )

    assert updates
    assert updates[-1]["final_response"]["content"]
    assert [message.role for message in updates[-1]["messages"]] == [
        "user",
        "assistant",
    ]
    assert {event.node_name for event in trace_store.events} >= {
        "intent_router",
        "planner_agent",
        "final_response",
    }


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
        rag_request = next(
            item
            for item in state["plan"]["requested_tools"]
            if item.tool_name == "rag_search"
        ).model_copy(update={"call_id": "call_ready_rag"})
        state["plan"] = {
            **state["plan"],
            "requested_tools": [request, rag_request],
        }
        state["ready_tool_call"] = ReadyToolCall(
            call_id=rag_request.call_id,
            step_index=1,
            request_fingerprint=tool_call_fingerprint(rag_request),
            tool_name=rag_request.tool_name,
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
        allow_test_tool_agent_override=True,
        retriever_node=fake_retriever,
        trace_store=trace_store,
        permission_scopes=["mcp:mock"],
    )

    node_names = [event.node_name for event in trace_store.events]

    assert result["tool_calls"][0].request.tool_name == "mcp.mock.stock.resolve_company"
    assert node_names.index("skill_agent") < node_names.index("tool_agent")
    assert node_names.index("tool_agent") < node_names.index("retriever_agent")
    assert result["verification"]["verdict"] == "pass"


def test_question_answer_graph_completes_a_share_company_snapshot():
    trace_store = InMemoryTraceStore()
    received_arguments: dict[str, dict] = {}

    class ResolveCompanyInput(BaseModel):
        query: str
        required_fields: list[str]
        optional_fields: list[str] = Field(default_factory=list)

    class CompanyProfileInput(BaseModel):
        symbol: str
        required_fields: list[str]
        optional_fields: list[str] = Field(default_factory=list)

    class MarketSnapshotInput(BaseModel):
        symbol: str
        required_fields: list[str]
        optional_fields: list[str] = Field(default_factory=list)

    class ResolveCompanyTool(Tool[ResolveCompanyInput]):
        name = "mcp.mock.stock.resolve_company"
        version = "test-v1"
        input_model = ResolveCompanyInput
        permission_scope = ["mcp:mock"]

        async def run(self, input_data: ResolveCompanyInput) -> ToolCallResult:
            received_arguments[self.name] = input_data.model_dump()
            return ToolCallResult(
                tool_call_id="test_resolve_company",
                tool_name=self.name,
                status="success",
                result={
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "exchange": "SSE",
                },
                trace_id="test_trace",
            )

    class CompanyProfileTool(Tool[CompanyProfileInput]):
        name = "mcp.mock.stock.company_profile"
        version = "test-v1"
        input_model = CompanyProfileInput
        permission_scope = ["mcp:mock"]

        async def run(self, input_data: CompanyProfileInput) -> ToolCallResult:
            received_arguments[self.name] = input_data.model_dump()
            return ToolCallResult(
                tool_call_id="test_company_profile",
                tool_name=self.name,
                status="success",
                result={
                    "symbol": input_data.symbol,
                    "name": "贵州茅台",
                    "industry": "食品饮料",
                    "main_business": "茅台酒及系列酒的生产与销售",
                    "board": "主板",
                    "registered_address": "贵州省仁怀市",
                },
                trace_id="test_trace",
            )

    class MarketSnapshotTool(Tool[MarketSnapshotInput]):
        name = "mcp.mock.stock.market_snapshot"
        version = "test-v1"
        input_model = MarketSnapshotInput
        permission_scope = ["mcp:mock"]

        async def run(self, input_data: MarketSnapshotInput) -> ToolCallResult:
            received_arguments[self.name] = input_data.model_dump()
            return ToolCallResult(
                tool_call_id="test_market_snapshot",
                tool_name=self.name,
                status="success",
                result={
                    "symbol": input_data.symbol,
                    "price": 1500.0,
                    "pe_ttm": 25.0,
                    "pb": 8.0,
                },
                trace_id="test_trace",
            )

    def market_tool_registry() -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(ResolveCompanyTool())
        registry.register(CompanyProfileTool())
        registry.register(MarketSnapshotTool())
        return registry

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
        permission_scopes=["mcp:mock"],
        tool_registry_factory=market_tool_registry,
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
    assert received_arguments["mcp.mock.stock.resolve_company"] == {
        "query": "贵州茅台",
        "required_fields": ["symbol", "name"],
        "optional_fields": ["exchange"],
    }
    assert received_arguments["mcp.mock.stock.company_profile"] == {
        "symbol": "600519.SH",
        "required_fields": ["name", "industry", "main_business"],
        "optional_fields": ["board", "registered_address"],
    }
    assert received_arguments["mcp.mock.stock.market_snapshot"] == {
        "symbol": "600519.SH",
        "required_fields": ["price"],
        "optional_fields": ["pe_ttm", "pb"],
    }
    assert "公司概览" in content
    assert "贵州茅台" in content
    assert "不构成投资建议" in content


def test_equity_graph_reports_unconfigured_market_data_without_fixture_values():
    result = run_question_answer_graph(
        session_id="market-unconfigured",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
        retriever_node=lambda state: state,
        skill_agent_node=_acceptance_market_skill_agent(required_fields=["price"]),
        tool_registry_factory=_acceptance_market_registry(
            error=ToolError(
                code="market_data_source_not_configured",
                message="尚未配置市场数据源。",
            )
        ),
    )

    content = result["final_response"]["content"]
    assert "未配置市场数据源" in content
    assert "1500" not in content
    assert result["verification"]["verdict"] == "execution_error"


def test_equity_graph_reports_required_pe_field_permission_gap_without_value():
    result = run_question_answer_graph(
        session_id="market-pe-permission-gap",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰并告诉我 PE",
        retriever_node=lambda state: state,
        skill_agent_node=_acceptance_market_skill_agent(required_fields=["pe_ttm"]),
        tool_registry_factory=_acceptance_market_registry(
            error=ToolError(
                code="required_api_permission_missing",
                message="当前数据源权限无法提供必需字段。",
                details={
                    "missing_fields": ["pe_ttm"],
                    "required_apis": ["daily_basic"],
                },
            )
        ),
    )

    content = result["final_response"]["content"]
    assert "当前数据源权限不足" in content
    assert "pe_ttm" in content
    assert "42.1" not in content
    assert result["draft_answer"]["business_error_code"] == (
        "required_api_permission_missing"
    )


def test_equity_graph_uses_daily_only_and_keeps_answer_bound_to_price_fact():
    payload = _market_data_envelope(
        status="complete",
        data={
            "symbol": "300085.SZ",
            "price": 18.23,
            "source_provider": "tushare",
            "as_of_date": "2026-07-25",
        },
        requested_fields=["price"],
        provided_fields=["price"],
        source_apis=["daily"],
    )
    result = run_question_answer_graph(
        session_id="market-price-daily-only",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰的最新价格",
        retriever_node=lambda state: state,
        skill_agent_node=_acceptance_market_skill_agent(required_fields=["price"]),
        tool_registry_factory=_acceptance_market_registry(payload=payload),
    )

    content = result["final_response"]["content"]
    assert "18.23" in content
    assert result["analysis"]["data_coverage"][0]["source_apis"] == ["daily"]
    assert result["draft_answer"]["claims"][0]["tool_evidence_refs"] == [
        {
            "tool_call_id": result["tool_calls"][0].call_id,
            "field_path": "data.price",
            "value": 18.23,
        }
    ]
    assert result["verification"]["verdict"] == "pass"


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
        user_message="分析一下银之杰",
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
    assert interrupt_payload["checkpoint_thread_id"] == result["session_key"]
    assert interrupt_payload["checkpoint_namespace"] == result["tenant_id"]
    assert retrieval_calls == []
    assert "retriever_agent" not in node_names

    resumed = resume_question_answer_graph(
        checkpoint_thread_id=interrupt_payload["checkpoint_thread_id"],
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
        user_message="分析一下银之杰",
        mock_evidence=False,
        skill_agent_node=fake_skill_agent,
        retriever_node=fake_retriever,
        checkpointer=rejected_checkpointer,
        permission_scopes=["rag:retrieve"],
    )
    rejected_payload = rejected_initial["__interrupt__"][0].value
    rejected = resume_question_answer_graph(
        checkpoint_thread_id=rejected_payload["checkpoint_thread_id"],
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
    assert all(
        approval.arguments_hash != old_approval["arguments_hash"]
        for approval in snapshot.values["approved_tool_calls"]
    )


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
        user_message="分析一下银之杰",
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


def test_approval_resume_rejects_changed_tool_catalog_for_same_tool_version():
    class WriteInput(BaseModel):
        value: str

    class CatalogWriteTool(Tool[WriteInput]):
        name = "mcp.mock.catalog_write"
        version = "1.0.0"
        input_model = WriteInput
        risk_level = "high"
        permission_scope = ["mcp:mock"]

        async def run(self, input_data: WriteInput) -> ToolCallResult:
            return ToolCallResult(
                tool_call_id="tool_catalog_write",
                tool_name=self.name,
                status="success",
                result={"value": input_data.value},
                trace_id="trace_tool",
            )

    def registry_factory():
        registry = ToolRegistry()
        registry.register(CatalogWriteTool())
        return registry

    def catalog_skill(state):
        state["selected_skill"] = {
            "skill_name": "catalog_skill",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": ["mcp.mock.catalog_write"],
        }
        state["plan"] = state["plan"] | {
            "tool_policy": {"allowed_tools": ["mcp.mock.catalog_write"]},
            "requested_tools": [
                ToolCallRequest(
                    tool_name="mcp.mock.catalog_write",
                    tool_version="1.0.0",
                    arguments={"value": "approved-only"},
                    purpose="验证恢复目录摘要",
                )
            ],
        }
        return state

    checkpointer = InMemorySaver()
    initial = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
        skill_agent_node=catalog_skill,
        checkpointer=checkpointer,
        permission_scopes=["mcp:mock"],
        tool_registry_factory=registry_factory,
    )
    approval = initial["__interrupt__"][0].value
    resume_trace_store = InMemoryTraceStore()

    def changed_registry_factory():
        tool = CatalogWriteTool()
        tool.risk_level = "low"
        tool.output_schema = {"type": "object", "required": ["changed"]}
        registry = ToolRegistry()
        registry.register(tool)
        return registry

    with pytest.raises(AgentGraphExecutionError) as exc_info:
        resume_question_answer_graph(
            checkpoint_thread_id=approval["checkpoint_thread_id"],
            approval_decision={
                "approval_id": approval["approval_id"],
                "arguments_hash": approval["arguments_hash"],
                "decision": "approved",
            },
            skill_agent_node=catalog_skill,
            trace_store=resume_trace_store,
            checkpointer=checkpointer,
            permission_scopes=["mcp:mock"],
            tool_registry_factory=changed_registry_factory,
        )

    assert exc_info.value.code == "tool_catalog_changed"
    event = resume_trace_store.events[0]
    assert event.node_name == "catalog_guard"
    assert event.status == "error"
    assert event.error == "tool_catalog_changed:ToolCatalogChangedError"
    snapshot = checkpointer.get_tuple(
        {"configurable": {"thread_id": initial["session_key"]}}
    )
    assert initial["tool_catalog_digest"] in repr(snapshot.checkpoint)


def test_approval_resume_rejects_changed_executor_binding():
    class WriteInput(BaseModel):
        value: str

    class ExecutorA(Tool[WriteInput]):
        name = "mcp.mock.binding_write"
        version = "1.0.0"
        input_model = WriteInput
        risk_level = "high"
        permission_scope = ["mcp:mock"]

        async def run(self, input_data: WriteInput) -> ToolCallResult:
            raise AssertionError("审批前不得执行 provider A")

    class ExecutorB(ExecutorA):
        async def run(self, input_data: WriteInput) -> ToolCallResult:
            raise AssertionError("旧审批不得执行 provider B")

    def registry_for(executor):
        registry = ToolRegistry(build_revision="build-1")
        registry.register(executor())
        return registry

    def binding_skill(state):
        state["selected_skill"] = {
            "skill_name": "binding_skill",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": [ExecutorA.name],
        }
        state["plan"] = state["plan"] | {
            "tool_policy": {"allowed_tools": [ExecutorA.name]},
            "requested_tools": [
                ToolCallRequest(
                    tool_name=ExecutorA.name,
                    tool_version=ExecutorA.version,
                    arguments={"value": "approved-only"},
                    purpose="验证 executor binding 恢复隔离",
                )
            ],
        }
        return state

    checkpointer = InMemorySaver()
    initial = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
        skill_agent_node=binding_skill,
        checkpointer=checkpointer,
        permission_scopes=["mcp:mock"],
        tool_registry_factory=lambda: registry_for(ExecutorA),
    )
    approval = initial["__interrupt__"][0].value

    with pytest.raises(AgentGraphExecutionError) as exc_info:
        resume_question_answer_graph(
            checkpoint_thread_id=approval["checkpoint_thread_id"],
            approval_decision={
                "approval_id": approval["approval_id"],
                "arguments_hash": approval["arguments_hash"],
                "decision": "approved",
            },
            skill_agent_node=binding_skill,
            checkpointer=checkpointer,
            permission_scopes=["mcp:mock"],
            tool_registry_factory=lambda: registry_for(ExecutorB),
        )

    assert exc_info.value.code == "tool_catalog_changed"


def test_approval_resume_rejects_changed_actual_rag_provider_binding():
    def provider_response(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"contexts": [], "query": {}, "metadata": {}})

    def rag_retriever(server_id):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(provider_response),
            base_url="http://rag",
        )
        tool = RagSearchTool(client=client)
        tool.risk_level = "high"
        return build_rag_retriever_node(
            tool=tool,
            execution_binding=ExecutionBinding(
                provider="rag",
                implementation_id="rag.retrieve",
                server_id=server_id,
                deployment_revision="deploy-1",
                config_revision="config-1",
            ),
        )

    def rag_skill(state):
        state["selected_skill"] = {
            "skill_name": "bound_rag_skill",
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
                    arguments={
                        "query": "敏感投研资料",
                        "tenant_id": "tenant_1",
                        "user_id": "user_1",
                    },
                    purpose="验证 RAG provider binding",
                )
            ],
        }
        return state

    checkpointer = InMemorySaver()
    initial = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
        retriever_node=rag_retriever("rag-a"),
        skill_agent_node=rag_skill,
        checkpointer=checkpointer,
        permission_scopes=["rag:retrieve"],
        tool_registry_factory=ToolRegistry,
    )
    approval = initial["__interrupt__"][0].value

    with pytest.raises(AgentGraphExecutionError) as exc_info:
        resume_question_answer_graph(
            checkpoint_thread_id=approval["checkpoint_thread_id"],
            approval_decision={
                "approval_id": approval["approval_id"],
                "arguments_hash": approval["arguments_hash"],
                "decision": "approved",
            },
            retriever_node=rag_retriever("rag-b"),
            skill_agent_node=rag_skill,
            checkpointer=checkpointer,
            permission_scopes=["rag:retrieve"],
            tool_registry_factory=ToolRegistry,
        )

    assert exc_info.value.code == "tool_catalog_changed"


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
        user_message="分析一下银之杰",
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
        checkpoint_thread_id=interrupt_payload["checkpoint_thread_id"],
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


def test_graph_rejects_schema_sensitive_arguments_before_approval():
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

    trace_store = InMemoryTraceStore()

    with pytest.raises(AgentGraphExecutionError) as exc_info:
        run_question_answer_graph(
            session_id="s1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="分析一下银之杰",
            skill_agent_node=secret_skill,
            tool_agent_node=build_tool_agent_node(registry_factory=registry_factory),
            checkpointer=InMemorySaver(),
            trace_store=trace_store,
        )

    assert exc_info.value.code == "inline_credentials_forbidden"
    assert "write-only-value" not in str(exc_info.value)
    assert "schema-sensitive-value" not in str(exc_info.value)
    events = [event for event in trace_store.events if event.node_name == "skill_agent"]
    assert [(event.status, event.error) for event in events] == [
        ("error", "inline_credentials_forbidden:InlineCredentialsForbiddenError")
    ]


@pytest.mark.parametrize("checkpoint_backend", ["memory", "sqlite"])
@pytest.mark.parametrize("request_version", ["1.0.0", "forged-version"])
def test_skill_outbound_guard_rejects_server_injected_fields_before_checkpoint(
    checkpoint_backend,
    request_version,
    tmp_path,
):
    class RuntimeInput(BaseModel):
        runtime_handle: str
        value: str

    class RuntimeInjectedTool(Tool[RuntimeInput]):
        name = "mcp.mock.runtime_injected"
        version = "1.0.0"
        input_model = RuntimeInput
        server_injected_fields = ["runtime_handle"]

        async def run(self, input_data: RuntimeInput) -> ToolCallResult:
            raise AssertionError("unsafe Skill output must never reach provider")

    registry = ToolRegistry()
    registry.register(RuntimeInjectedTool())
    if checkpoint_backend == "memory":
        checkpointer = InMemorySaver()
    else:
        checkpointer = build_graph_checkpointer(
            backend="sqlite",
            sqlite_path=str(tmp_path / "skill-guard.sqlite"),
        )

    def unsafe_skill(state):
        state["selected_skill"] = {
            "skill_name": "unsafe_skill",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": [RuntimeInjectedTool.name],
        }
        state["plan"] = (state["plan"] or {}) | {
            "tool_policy": {"allowed_tools": [RuntimeInjectedTool.name]},
            "requested_tools": [
                ToolCallRequest.model_construct(
                    tool_name=RuntimeInjectedTool.name,
                    tool_version=request_version,
                    arguments={
                        "runtime_handle": "checkpoint-secret",
                        "value": "safe-value",
                    },
                    purpose="验证 Skill checkpoint 前校验",
                )
            ],
        }
        return state

    state = create_initial_state(
        session_id="s_skill_guard",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
    )
    graph = build_question_answer_graph(
        retriever_node=lambda current: current,
        skill_agent_node=unsafe_skill,
        checkpointer=checkpointer,
        tool_registry_factory=lambda: registry,
    )
    config = {"configurable": {"thread_id": state["request_id"]}}

    with pytest.raises(AgentGraphExecutionError) as exc_info:
        graph.invoke(state, config=config)

    assert exc_info.value.code == "inline_credentials_forbidden"
    checkpoint_state = graph.get_state(config).values
    assert "checkpoint-secret" not in repr(checkpoint_state)
    assert all(
        "checkpoint-secret" not in repr(checkpoint.checkpoint)
        for checkpoint in checkpointer.list(config)
    )
    if checkpoint_backend == "sqlite":
        checkpointer.conn.close()
        build_graph_checkpointer.cache_clear()


@pytest.mark.parametrize("checkpoint_backend", ["memory", "sqlite"])
def test_skill_url_query_credentials_never_enter_checkpoint(
    checkpoint_backend,
    tmp_path,
):
    if checkpoint_backend == "memory":
        checkpointer = InMemorySaver()
    else:
        checkpointer = build_graph_checkpointer(
            backend="sqlite",
            sqlite_path=str(tmp_path / "query-credential.sqlite"),
        )

    def unsafe_skill(state):
        state["plan"] = (state["plan"] or {}) | {
            "tool_policy": {"allowed_tools": ["callback"]},
            "requested_tools": [
                ToolCallRequest.model_construct(
                    tool_name="callback",
                    arguments={
                        "url": "https://example.com/cb?REFRESH_TOKEN=query-checkpoint-secret"
                    },
                    purpose="验证 query 凭证不得入 checkpoint",
                )
            ],
        }
        return state

    state = create_initial_state(
        session_id="s_query_guard",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
    )
    graph = build_question_answer_graph(
        retriever_node=lambda current: current,
        skill_agent_node=unsafe_skill,
        checkpointer=checkpointer,
        tool_registry_factory=ToolRegistry,
    )
    config = {"configurable": {"thread_id": state["request_id"]}}

    with pytest.raises(AgentGraphExecutionError) as exc_info:
        graph.invoke(state, config=config)

    assert exc_info.value.code == "inline_credentials_forbidden"
    assert all(
        "query-checkpoint-secret" not in repr(checkpoint.checkpoint)
        for checkpoint in checkpointer.list(config)
    )
    if checkpoint_backend == "sqlite":
        checkpointer.conn.close()
        build_graph_checkpointer.cache_clear()


@pytest.mark.parametrize("checkpoint_backend", ["memory", "sqlite"])
def test_tool_provider_exception_is_safe_in_checkpoint_trace_and_final(
    checkpoint_backend,
    tmp_path,
):
    class FailureInput(BaseModel):
        value: str

    class FailingTool(Tool[FailureInput]):
        name = "mcp.mock.safe_failure"
        version = "1.0.0"
        input_model = FailureInput

        async def run(self, input_data: FailureInput) -> ToolCallResult:
            raise RuntimeError(
                "Authorization: Basic dXNlcjpwYXNz "
                "https://api.test/cb?access_token=tool-checkpoint-secret"
            )

    registry = ToolRegistry()
    registry.register(FailingTool())
    if checkpoint_backend == "memory":
        checkpointer = InMemorySaver()
    else:
        checkpointer = build_graph_checkpointer(
            backend="sqlite",
            sqlite_path=str(tmp_path / "tool-failure.sqlite"),
        )
    trace_store = InMemoryTraceStore()

    def failing_skill(state):
        state["selected_skill"] = {
            "skill_name": "failing_skill",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": [FailingTool.name],
        }
        state["plan"] = (state["plan"] or {}) | {
            "tool_policy": {"allowed_tools": [FailingTool.name]},
            "requested_tools": [
                ToolCallRequest(
                    tool_name=FailingTool.name,
                    tool_version=FailingTool.version,
                    arguments={"value": "safe"},
                    purpose="验证异常安全归一化",
                )
            ],
        }
        return state

    state = create_initial_state(
        session_id="s_tool_failure",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
    )
    graph = build_question_answer_graph(
        retriever_node=lambda current: current,
        skill_agent_node=failing_skill,
        checkpointer=checkpointer,
        trace_store=trace_store,
        tool_registry_factory=lambda: registry,
    )
    config = {"configurable": {"thread_id": state["request_id"]}}

    result = graph.invoke(state, config=config)

    assert result["final_response"]["response_type"] == "execution_error_response"
    assert result["final_response"]["business_error_code"] == "tool_execution_failed"
    serialized = repr(result)
    serialized += repr(trace_store.list_events(result["trace_id"]))
    serialized += repr(trace_store.list_artifacts(result["trace_id"]))
    serialized += "".join(
        repr(checkpoint.checkpoint) for checkpoint in checkpointer.list(config)
    )
    assert "tool-checkpoint-secret" not in serialized
    assert "dXNlcjpwYXNz" not in serialized
    if checkpoint_backend == "sqlite":
        checkpointer.conn.close()
        build_graph_checkpointer.cache_clear()


@pytest.mark.parametrize("checkpoint_backend", ["memory", "sqlite"])
def test_malformed_result_after_approved_side_effect_is_checkpointed_without_replay(
    checkpoint_backend,
    tmp_path,
):
    class WriteInput(BaseModel):
        value: str

    provider_calls = []

    class MalformedWriteTool(Tool[WriteInput]):
        name = "mcp.mock.malformed_write"
        version = "1.0.0"
        input_model = WriteInput
        risk_level = "high"

        async def run(self, input_data: WriteInput):
            provider_calls.append(input_data.value)
            return {
                "status": "success",
                "result": {
                    "database_password": "post-side-effect-checkpoint-secret"
                },
            }

    registry = ToolRegistry(build_revision="build-1")
    registry.register(MalformedWriteTool())
    if checkpoint_backend == "memory":
        checkpointer = InMemorySaver()
    else:
        checkpointer = build_graph_checkpointer(
            backend="sqlite",
            sqlite_path=str(tmp_path / "malformed-side-effect.sqlite"),
        )
    trace_store = InMemoryTraceStore()

    def write_skill(state):
        state["selected_skill"] = {
            "skill_name": "malformed_write_skill",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": [MalformedWriteTool.name],
        }
        state["plan"] = (state["plan"] or {}) | {
            "tool_policy": {"allowed_tools": [MalformedWriteTool.name]},
            "requested_tools": [
                ToolCallRequest(
                    tool_name=MalformedWriteTool.name,
                    tool_version=MalformedWriteTool.version,
                    arguments={"value": "write-once"},
                    purpose="验证副作用后非法 DTO 不重试",
                )
            ],
        }
        return state

    initial = run_question_answer_graph(
        session_id="s_malformed_write",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
        skill_agent_node=write_skill,
        checkpointer=checkpointer,
        trace_store=trace_store,
        tool_registry_factory=lambda: registry,
    )
    approval = initial["__interrupt__"][0].value

    resumed = resume_question_answer_graph(
        checkpoint_thread_id=approval["checkpoint_thread_id"],
        approval_decision={
            "approval_id": approval["approval_id"],
            "arguments_hash": approval["arguments_hash"],
            "decision": "approved",
        },
        skill_agent_node=write_skill,
        checkpointer=checkpointer,
        trace_store=trace_store,
        tool_registry_factory=lambda: registry,
    )

    assert provider_calls == ["write-once"]
    assert resumed["final_response"]["response_type"] == "execution_error_response"
    assert resumed["final_response"]["business_error_code"] == (
        "tool_result_validation_failed"
    )
    assert resumed["ready_tool_call"] is None
    serialized = repr(resumed)
    serialized += repr(trace_store.list_events(resumed["trace_id"]))
    serialized += repr(trace_store.list_artifacts(resumed["trace_id"]))
    serialized += "".join(
        repr(checkpoint.checkpoint)
        for checkpoint in checkpointer.list(
            {"configurable": {"thread_id": initial["session_key"]}}
        )
    )
    assert "post-side-effect-checkpoint-secret" not in serialized

    graph = build_question_answer_graph(
        retriever_node=lambda state: state,
        skill_agent_node=write_skill,
        checkpointer=checkpointer,
        trace_store=trace_store,
        tool_registry_factory=lambda: registry,
    )
    replayed = graph.invoke(
        None,
        config={"configurable": {"thread_id": initial["session_key"]}},
    )
    assert replayed["final_response"]["response_type"] == "execution_error_response"
    assert provider_calls == ["write-once"]
    if checkpoint_backend == "sqlite":
        checkpointer.conn.close()
        build_graph_checkpointer.cache_clear()


@pytest.mark.parametrize("checkpoint_backend", ["memory", "sqlite"])
def test_malicious_failed_result_identifiers_never_enter_runtime_artifacts(
    checkpoint_backend,
    tmp_path,
):
    class FailureInput(BaseModel):
        value: str

    class MaliciousFailureTool(Tool[FailureInput]):
        name = "mcp.mock.malicious_failure"
        version = "1.0.0"
        input_model = FailureInput

        async def run(self, _):
            return ToolCallResult(
                tool_call_id="database_password=runtime-call-secret",
                tool_name="Bearer runtime-name-secret",
                status="failed",
                result={"client_secret": "runtime-result-secret"},
                error={
                    "code": "provider_failed",
                    "message": "client_secret=runtime-error-secret",
                },
                latency_ms=999,
                trace_id="database_password=runtime-trace-secret",
            )

    registry = ToolRegistry(build_revision="build-1")
    registry.register(MaliciousFailureTool())
    if checkpoint_backend == "memory":
        checkpointer = InMemorySaver()
    else:
        checkpointer = build_graph_checkpointer(
            backend="sqlite",
            sqlite_path=str(tmp_path / "malicious-result.sqlite"),
        )
    trace_store = InMemoryTraceStore()

    def failure_skill(state):
        state["selected_skill"] = {
            "skill_name": "malicious_failure_skill",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": [MaliciousFailureTool.name],
        }
        state["plan"] = (state["plan"] or {}) | {
            "tool_policy": {"allowed_tools": [MaliciousFailureTool.name]},
            "requested_tools": [
                ToolCallRequest(
                    tool_name=MaliciousFailureTool.name,
                    arguments={"value": "safe"},
                    purpose="验证恶意失败标识不会持久化",
                )
            ],
        }
        return state

    state = create_initial_state(
        session_id="s_malicious_failure",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
    )
    graph = build_question_answer_graph(
        retriever_node=lambda current: current,
        skill_agent_node=failure_skill,
        checkpointer=checkpointer,
        trace_store=trace_store,
        tool_registry_factory=lambda: registry,
    )
    config = {"configurable": {"thread_id": state["request_id"]}}
    result = graph.invoke(state, config=config)

    saved_result = result["tool_calls"][0].result
    assert saved_result.tool_name == MaliciousFailureTool.name
    assert saved_result.trace_id == result["trace_id"]
    assert saved_result.tool_call_id.startswith("tool_")
    serialized = repr(result)
    serialized += repr(trace_store.list_events(result["trace_id"]))
    serialized += repr(trace_store.list_artifacts(result["trace_id"]))
    serialized += "".join(
        repr(checkpoint.checkpoint) for checkpoint in checkpointer.list(config)
    )
    for secret in (
        "runtime-call-secret",
        "runtime-name-secret",
        "runtime-result-secret",
        "runtime-error-secret",
        "runtime-trace-secret",
    ):
        assert secret not in serialized
    if checkpoint_backend == "sqlite":
        checkpointer.conn.close()
        build_graph_checkpointer.cache_clear()


@pytest.mark.parametrize("checkpoint_backend", ["memory", "sqlite"])
def test_retriever_failure_consumes_ready_call_and_checkpoint_prevents_replay(
    checkpoint_backend,
    tmp_path,
):
    provider_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        return httpx.Response(
            503,
            json={"detail": "database_password: provider-checkpoint-secret"},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://rag",
    )
    retriever = build_rag_retriever_node(
        tool=RagSearchTool(client=client),
        execution_binding=ExecutionBinding(
            provider="rag",
            implementation_id="rag.test.failure",
            server_id="rag-test",
            deployment_revision="test-1",
            config_revision="test",
        ),
    )
    if checkpoint_backend == "memory":
        checkpointer = InMemorySaver()
    else:
        checkpointer = build_graph_checkpointer(
            backend="sqlite",
            sqlite_path=str(tmp_path / "retriever-failure.sqlite"),
        )

    def rag_skill(state):
        state["selected_skill"] = {
            "skill_name": "failing_rag_skill",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": ["rag_search"],
        }
        state["plan"] = (state["plan"] or {}) | {
            "tool_policy": {"allowed_tools": ["rag_search"]},
            "requested_tools": [
                ToolCallRequest(
                    tool_name="rag_search",
                    tool_version="1.0.0",
                    arguments={
                        "query": "检索故障测试",
                        "tenant_id": "tenant_1",
                        "user_id": "user_1",
                        "top_k": 3,
                    },
                    purpose="验证失败后不重放",
                )
            ],
        }
        return state

    registry = ToolRegistry()
    state = create_initial_state(
        session_id="s_retriever_failure",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
        permission_scopes=["rag:retrieve"],
    )
    graph = build_question_answer_graph(
        retriever_node=retriever,
        skill_agent_node=rag_skill,
        checkpointer=checkpointer,
        tool_registry_factory=lambda: registry,
    )
    config = {"configurable": {"thread_id": state["request_id"]}}

    result = graph.invoke(state, config=config)

    assert result["ready_tool_call"] is None
    assert result["tool_calls"][0].result.error.code == "rag_index_unavailable"
    assert result["errors"][-1]["code"] == "rag_index_unavailable"
    assert result["errors"][-1]["http_status"] == 503
    assert "provider-checkpoint-secret" not in repr(result)
    assert "检索服务暂不可用" in result["final_response"]["content"]
    assert result["draft_answer"]["claims"] == []
    assert result["draft_answer"]["cited_evidence_ids"] == []
    assert result["draft_answer"]["response_type"] == "execution_error_response"
    assert result["draft_answer"]["business_error_code"] == "rag_index_unavailable"
    assert result["verification"]["verdict"] == "execution_error"
    assert result["approval_status"].status == "not_required"
    assert result["approval_requests"] == []
    assert result["final_response"]["response_type"] == "execution_error_response"
    assert result["final_response"]["business_error_code"] == "rag_index_unavailable"
    assert len(provider_calls) == 1
    snapshot = graph.get_state(config)
    assert snapshot.values["ready_tool_call"] is None
    assert "provider-checkpoint-secret" not in repr(snapshot.values)

    replay_result = graph.invoke(None, config=config)

    assert replay_result["ready_tool_call"] is None
    assert len(provider_calls) == 1
    if checkpoint_backend == "sqlite":
        checkpointer.conn.close()
        build_graph_checkpointer.cache_clear()


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
        user_message="分析一下银之杰",
        skill_agent_node=skill,
        tool_agent_node=tool_agent,
        checkpointer=checkpointer,
    )
    first_approval = initial["__interrupt__"][0].value

    assert first_approval["tool_name"] == "mcp.mock.first_write"
    assert first_calls == []
    assert second_calls == []

    after_first = resume_question_answer_graph(
        checkpoint_thread_id=first_approval["checkpoint_thread_id"],
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
        checkpoint_thread_id=second_approval["checkpoint_thread_id"],
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
    state["tool_catalog_digest"] = registry_factory().catalog_digest

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
        user_message="分析一下银之杰",
        skill_agent_node=skill,
        tool_agent_node=tool_agent,
        checkpointer=checkpointer,
    )
    first = initial["__interrupt__"][0].value
    after_first = resume_question_answer_graph(
        checkpoint_thread_id=first["checkpoint_thread_id"],
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
        checkpoint_thread_id=second["checkpoint_thread_id"],
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
    state["tool_catalog_digest"] = registry_factory().catalog_digest

    result = build_tool_agent_node(registry_factory=registry_factory)(state)

    assert result["tool_calls"][0].result.status == "failed"
    assert result["pending_tool_approval"] is None
    assert write_calls == []


def test_failed_prerequisite_does_not_route_later_rag_request():
    class LookupInput(BaseModel):
        value: str

    retriever_calls = []

    class FailingLookupTool(Tool[LookupInput]):
        name = "mcp.mock.failed_before_rag"
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

    def registry_factory():
        registry = ToolRegistry()
        registry.register(FailingLookupTool())
        return registry

    def skill(state):
        allowed = ["mcp.mock.failed_before_rag", "rag_search"]
        state["selected_skill"] = {
            "skill_name": "failed_then_rag",
            "skill_version": "1.0.0",
            "status": "ready",
            "allowed_tools": allowed,
        }
        state["plan"] = state["plan"] | {
            "tool_policy": {"allowed_tools": allowed},
            "requested_tools": [
                ToolCallRequest(
                    tool_name="mcp.mock.failed_before_rag",
                    arguments={"value": "target"},
                    purpose="失败的前置调用",
                ),
                ToolCallRequest(
                    tool_name="rag_search",
                    arguments={"query": "不得执行的检索"},
                    purpose="后续检索",
                ),
            ],
        }
        return state

    def retriever(state):
        retriever_calls.append(state["request_id"])
        return state

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下银之杰",
        skill_agent_node=skill,
        tool_agent_node=build_tool_agent_node(registry_factory=registry_factory),
        retriever_node=retriever,
    )

    assert result["tool_calls"][0].result.status == "failed"
    assert result.get("ready_tool_call") is None
    assert retriever_calls == []


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
        user_message="分析一下银之杰",
        skill_agent_node=skill,
        tool_agent_node=tool_agent,
        retriever_node=retriever,
        checkpointer=checkpointer,
        permission_scopes=["rag:retrieve", "portfolio:read"],
    )

    approval = initial["__interrupt__"][0].value
    assert approval["tool_name"] == "rag_search"
    assert approval["call_id"]
    assert retriever_calls == []
    assert provider_calls == []

    completed = resume_question_answer_graph(
        checkpoint_thread_id=approval["checkpoint_thread_id"],
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
    assert completed["permission_scopes"] == ["rag:retrieve", "portfolio:read"]
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
        user_message="分析一下银之杰",
        skill_agent_node=skill,
        retriever_node=retriever,
        checkpointer=checkpointer,
    )
    approval = initial["__interrupt__"][0].value

    completed = resume_question_answer_graph(
        checkpoint_thread_id=approval["checkpoint_thread_id"],
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
    assert [item.call_id for item in completed["approved_tool_calls"]] == [
        approval["call_id"]
    ]
    build_graph_checkpointer.cache_clear()


def test_question_answer_graph_loads_injected_memory_retriever_context():
    def retrieve_memory(query: str, *, tenant_id: str, user_id: str) -> EvidencePackage:
        assert query == "你好"
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
        user_message="你好",
        mock_evidence=True,
        memory_retriever=retrieve_memory,
    )

    assert result["memory_context"].entries == ["用户偏好：所有回答用中文"]
    assert result["memory_context"].sources == ["experience/memcand_1"]
