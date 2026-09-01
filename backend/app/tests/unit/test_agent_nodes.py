import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langgraph.runtime import Runtime
from pydantic import BaseModel

from app.graph.nodes.approval_coordinator import approval_coordinator_node
from app.graph.nodes.approval_gate import approval_gate_node, build_approval_action
from app.graph.nodes.analysis_agent import analysis_agent_node
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.nodes.skill_agent import build_skill_agent_node
from app.graph.nodes.synthesizer import (
    FINANCIAL_DISCLAIMER,
    PARTIAL_MARKET_DATA_DISCLOSURE,
    _equity_claims_template,
    build_synthesizer_node,
    synthesizer_node,
)
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.nodes.verifier import verifier_node
from app.graph.state import create_initial_state
from app.schemas.memory import MemoryCandidate
from app.schemas.evidence import EvidenceItem, EvidencePackage
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult, ToolError
from app.schemas.tools import ToolSpec
from app.llm.schemas import LlmCompletionResult
from app.skills.base import ApprovalPolicy, Skill, SkillOutput, SkillSpec
from app.skills.registry import SkillRegistry
from app.tools.base import Tool
from app.tools.registry import ToolRegistry
from app.tools.redaction import InlineCredentialsForbiddenError


def _preference_runtime(*preferences: tuple[str, str]) -> Runtime:
    store = Mock()
    store.search.return_value = [
        SimpleNamespace(
            key=memory_key,
            value={
                "memory_type": "user_preference",
                "memory_key": memory_key,
                "content": content,
            },
        )
        for memory_key, content in preferences
    ]
    return Runtime(store=store)


def test_planner_node_accepts_langgraph_runtime_without_store():
    state = create_initial_state(
        session_id="runtime-planner",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="请记住：以后回答简洁。",
    )
    state["intent"] = {"intent": "conversation", "risk_level": "low"}

    result = build_planner_agent_node(available_tools=[])(state, Runtime(store=None))

    assert result["plan"]["execution_mode"] == "direct_response"
    assert result["memory_candidates"][0].memory_key == "response_detail"


def test_memory_approval_action_digest_is_stable_without_exposing_content():
    state = create_initial_state(
        session_id="approval-memory",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="请记住我的联系电话。",
        request_id="run-1",
    )
    candidate = MemoryCandidate(
        candidate_id="candidate-1",
        memory_type="user_preference",
        memory_key="contact",
        content="13800138000",
        confidence=0.9,
        approval_status="pending",
        sensitivity="high",
    )
    state["memory_candidates"] = [candidate]

    first = build_approval_action(state)
    second = build_approval_action(state)

    assert first is not None
    assert first.approval_type == "memory_write"
    assert first.subject_name == "contact"
    assert first.action_digest == second.action_digest
    assert "13800138000" not in first.action_digest
    assert first.interrupt_payload["candidate_id"] == "candidate-1"


def test_memory_approval_rejection_marks_candidate_rejected(monkeypatch):
    state = create_initial_state(
        session_id="approval-memory-reject",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="请记住我的联系电话。",
        request_id="run-1",
    )
    state["memory_candidates"] = [
        MemoryCandidate(
            candidate_id="candidate-1",
            memory_type="user_preference",
            memory_key="contact",
            content="13800138000",
            confidence=0.9,
            approval_status="pending",
            sensitivity="high",
        )
    ]
    action = build_approval_action(state)
    monkeypatch.setattr(
        "app.graph.nodes.approval_gate.interrupt",
        lambda _payload: {
            "approval_id": action.interrupt_payload["approval_id"],
            "action_digest": action.action_digest,
            "decision": "rejected",
        },
    )

    result = approval_gate_node(state)

    assert result["memory_candidates"][0].approval_status == "rejected"
    assert result["approval_status"] is None


def test_synthesizer_node_accepts_langgraph_runtime_without_store():
    state = create_initial_state(
        session_id="runtime-synthesizer",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="你好",
    )
    state["plan"] = {"execution_mode": "direct_response", "response_policy": {}}

    result = build_synthesizer_node()(state, Runtime(store=None))

    assert result["draft_answer"] is not None


def test_planner_keeps_focus_domain_as_planning_preference():
    state = create_initial_state(
        session_id="planning-preference",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析一下公司",
    )
    state["intent"] = {"intent": "conversation", "risk_level": "low"}

    result = build_planner_agent_node(available_tools=[])(
        state,
        _preference_runtime(("focus_domain", "重点关注半导体行业")),
    )

    assert result["plan"]["preference_constraints"]["focus_domain"] == (
        "重点关注半导体行业"
    )
    assert "focus_domain" not in result["plan"]["response_policy"]


def test_synthesizer_excludes_focus_domain_from_presentation_policy():
    state = create_initial_state(
        session_id="presentation-preference",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="你好",
    )
    state["plan"] = {"execution_mode": "direct_response", "response_policy": {}}

    result = build_synthesizer_node()(
        state,
        _preference_runtime(
            ("focus_domain", "重点关注半导体行业"),
            ("language", "使用英文回答"),
        ),
    )

    response_policy = result["draft_answer"]["response_policy"]
    assert response_policy["language"] == "english"
    assert "focus_domain" not in response_policy


def test_equity_deterministic_fallback_groups_facts_and_guides_next_research_step():
    claims = [
        {
            "claim_id": "tool_claim_1",
            "text": "所属行业为软件服务",
            "evidence_ids": [],
            "tool_evidence_refs": [
                {"tool_call_id": "call_1", "field_path": "data.industry", "value": "软件服务"}
            ],
        },
        {
            "claim_id": "tool_claim_2",
            "text": "归母净利润为55667788",
            "evidence_ids": [],
            "tool_evidence_refs": [
                {
                    "tool_call_id": "call_2",
                    "field_path": "data.metrics.net_profit_parent",
                    "value": 55667788,
                }
            ],
        },
        {
            "claim_id": "tool_claim_3",
            "text": "市盈率（TTM）为91.2",
            "evidence_ids": [],
            "tool_evidence_refs": [
                {"tool_call_id": "call_3", "field_path": "data.pe_ttm", "value": 91.2}
            ],
        },
    ]

    rendered = _equity_claims_template(
        claims=claims,
        response_type="equity_research_report",
    )

    business_section = rendered.split("业务与行业\n", 1)[1].split("\n\n财务质量", 1)[0]
    financial_section = rendered.split("财务质量\n", 1)[1].split("\n\n行情与估值", 1)[0]
    market_section = rendered.split("行情与估值\n", 1)[1].split("\n\n主要风险", 1)[0]
    assert "软件服务" in business_section
    assert "归母净利润" not in business_section
    assert "归母净利润" in financial_section
    assert "市盈率" in market_section
    assert "下一步" in rendered


class RecordingSkill(Skill):
    spec = SkillSpec(
        name="recording_skill",
        version="2.0.0",
        description="记录运行时输入的测试 skill",
        supported_intents=["knowledge_qa"],
        required_inputs=["user_message"],
        risk_level="low",
        approval_policy=ApprovalPolicy(require_human=False),
        allowed_tools=["rag_search"],
    )

    def __init__(self) -> None:
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return SkillOutput(
            skill_name=self.spec.name,
            skill_version=self.spec.version,
            status="needs_retrieval",
            requested_tools=[
                ToolCallRequest(
                    tool_name="rag_search",
                    tool_version="1.0.0",
                    arguments={
                        "query": kwargs["user_message"],
                        "tenant_id": kwargs["tenant_id"],
                        "user_id": kwargs["user_id"],
                        "top_k": 3,
                        "retrieval_mode": "hybrid",
                    },
                    purpose="测试 skill 请求检索",
                )
            ],
            reason_codes=["missing_evidence"],
        )


class RecordingLlmClient:
    def __init__(self, content: str = "LLM 渲染后的回答") -> None:
        self.content = content
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(
            content=self.content,
            model="fake-model",
            provider="fake",
            usage={"total_tokens": 8},
        )


class SequencedLlmClient:
    def __init__(self, contents: list[str]) -> None:
        self.contents = iter(contents)
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(
            content=next(self.contents),
            model="fake-research-model",
            provider="fake",
            usage={"total_tokens": 16},
        )


class SequencedFailingLlmClient:
    def __init__(self, outcomes: list[str | Exception]) -> None:
        self.outcomes = iter(outcomes)
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return LlmCompletionResult(
            content=outcome,
            model="fake-research-model",
            provider="fake",
            usage={"total_tokens": 16},
        )


def _complete_equity_research_report(*, company_name: str = "银之杰") -> str:
    return (
        f"核心判断\n当前仅能确认公司名称为{company_name}。[tool_claim_1]\n\n"
        f"业务与行业\n公司名称为{company_name}。[tool_claim_1]\n\n"
        "财务质量\n财务数据未获得，无法判断。\n\n"
        "行情与估值\n行情与估值数据未获得，无法判断。\n\n"
        "主要风险\n风险证据未获得，无法判断。\n\n"
        "数据边界\n当前数据不足，需补充更多可靠资料。"
    )


def _minimal_claim_bound_equity_report(*, company_name: str = "银之杰") -> str:
    return _complete_equity_research_report(company_name=company_name)


class InvalidSchemaToolPlanningLlmClient(RecordingLlmClient):
    def __init__(self) -> None:
        super().__init__(
            """
            {
              "tool_name": "mcp.mock.invalid_tool",
              "tool_version": "1.0.0",
              "arguments": {"value": "test"},
              "purpose": "验证无效工具 Schema",
              "approval_required": false
            }
            """
        )


class InlineCredentialPlanningLlmClient(RecordingLlmClient):
    def __init__(self) -> None:
        super().__init__(
            """
            {
              "arguments": {"client_secret": "plaintext-secret"},
              "purpose": "尝试把凭证写入计划"
            }
            """
        )


class ServerInjectedPlanningLlmClient(RecordingLlmClient):
    def __init__(self) -> None:
        super().__init__(
            """
            {
              "arguments": {"runtime_handle": "model-supplied-value"},
              "purpose": "尝试写入服务端注入参数"
            }
            """
        )


class PePlanningLlmClient(RecordingLlmClient):
    def __init__(self) -> None:
        super().__init__(
            """
            {
              "arguments": {
                "symbol": "300085.SZ",
                "required_fields": ["pe_ttm"],
                "optional_fields": ["price"]
              },
              "purpose": "查询银之杰的 PE"
            }
            """
        )


class InvalidFieldPlanningLlmClient(RecordingLlmClient):
    def __init__(self, *, required_fields, optional_fields) -> None:
        super().__init__(
            json.dumps(
                {
                    "arguments": {
                        "symbol": "300085.SZ",
                        "required_fields": required_fields,
                        "optional_fields": optional_fields,
                    },
                    "purpose": "验证字段选择约束",
                },
                ensure_ascii=False,
            )
        )


class EquityResearchPlanningLlmClient(RecordingLlmClient):
    def __init__(self) -> None:
        super().__init__(
            json.dumps(
                {
                    "company_query": "银之杰",
                    "analysis_dimensions": [
                        "business",
                        "market",
                        "valuation",
                        "financial",
                        "risk",
                    ],
                    "need_mcp": True,
                    "need_rag": True,
                    "answer_depth": "standard",
                },
                ensure_ascii=False,
            )
        )


def _stock_tool_spec(name: str) -> ToolSpec:
    field_schema = {
        "type": "array",
        "items": {"type": "string"},
        "uniqueItems": True,
    }
    properties = {
        "required_fields": field_schema,
        "optional_fields": field_schema,
    }
    required = ["required_fields"]
    if name.endswith(".resolve_company"):
        properties["query"] = {"type": "string"}
        required = ["query", "required_fields"]
    else:
        properties["symbol"] = {"type": "string"}
        required = ["symbol", "required_fields"]
        if name.endswith(".financial_metrics"):
            properties["period"] = {"type": "string"}
        if name.endswith(".announcement_search"):
            properties["date_range"] = {"type": "string"}
        if name.endswith(".price_trend"):
            properties["window_days"] = {"type": "integer", "minimum": 20, "maximum": 250}
    return ToolSpec(
        name=name,
        version="mcp-http-v1",
        description=f"测试股票工具 {name}",
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    )


def test_planner_prioritizes_validated_image_context_over_knowledge_rag():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析这张财报截图",
    )
    state["image_analyses"] = [
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
            "chart_observations": [],
            "entities": [],
            "risk_flags": ["缺少审计意见"],
            "confidence": 0.9,
            "truncated": False,
        }
    ]
    state["intent"] = {
        "intent": "knowledge_qa",
        "domain": "a_share_equity_research",
        "risk_level": "medium",
        "confidence": 0.8,
    }

    result = build_planner_agent_node(available_tools=[])(state)

    assert result["plan"]["execution_mode"] == "image_analysis"
    assert result["plan"]["retrieval"]["required"] is False
    assert result["plan"]["response_policy"]["disclaimer_required"] is True


def test_planner_routes_financial_image_workflow_through_selected_skill():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="结合这张财报分析公司基本面",
    )
    state["image_analyses"] = [
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
    state["intent"] = {
        "intent": "task_workflow",
        "domain": "a_share_equity_research",
        "sub_intent": "financial_report_analysis",
        "candidate_skill": "financial_report_analysis",
        "risk_level": "medium",
        "confidence": 0.9,
    }

    result = build_planner_agent_node(available_tools=[])(state)

    assert result["plan"]["execution_mode"] == "skill_execution"
    assert result["plan"]["selected_skill"]["name"] == "financial_report_analysis"
    assert result["plan"]["response_policy"]["image_context_required"] is True
    assert "analyze_image_context" in result["plan"]["execution_steps"]


def test_planner_agent_builds_direct_response_plan_for_conversation_intent():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="你好",
    )
    state["intent"] = {
        "intent": "conversation",
        "risk_level": "low",
        "needs_clarification": False,
        "candidate_skill": None,
        "confidence": 0.9,
        "reason_codes": ["rule_conversation"],
        "next_node": "planner_agent",
    }

    result = build_planner_agent_node(available_tools=[])(state)

    assert result["plan"]["execution_mode"] == "direct_response"
    assert result["plan"]["selected_skill"] is None
    assert result["plan"]["retrieval"]["required"] is False
    assert result["plan"]["response_policy"]["citation_required"] is False
    assert result["plan"]["response_policy"]["response_type"] == "conversation_response"
    assert result["plan"]["response_policy"]["use_llm"] is True


def _market_snapshot_tool_spec() -> ToolSpec:
    field_schema = {
        "type": "array",
        "items": {
            "type": "string",
            "enum": ["price", "pct_change", "turnover", "market_cap", "pe_ttm", "pb"],
        },
        "uniqueItems": True,
    }
    return ToolSpec(
        name="mcp.market.stock.market_snapshot",
        version="mcp-http-v1",
        description="查询 A 股行情和估值快照",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "required_fields": field_schema,
                "optional_fields": field_schema,
            },
            "required": ["symbol", "required_fields"],
            "additionalProperties": False,
        },
        risk_level="low",
    )


def test_planner_marks_user_requested_pe_as_required_field():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="查一下银之杰的 PE",
    )
    state["intent"] = {
        "intent": "tool_action",
        "risk_level": "low",
        "candidate_tool": "mcp.market.stock.market_snapshot",
    }

    result = build_planner_agent_node(
        llm_client=PePlanningLlmClient(),
        available_tools=[_market_snapshot_tool_spec()],
    )(state)

    request = result["plan"]["requested_tools"][0]
    assert request.arguments["required_fields"] == ["pe_ttm"]
    assert request.arguments["optional_fields"] == ["price"]


@pytest.mark.parametrize(
    ("required_fields", "optional_fields"),
    [
        (["unknown_metric"], []),
        (["pe_ttm"], ["pe_ttm"]),
        ([], []),
    ],
    ids=["unknown", "overlap", "empty"],
)
def test_planner_falls_back_without_tool_request_for_invalid_field_selection(
    required_fields,
    optional_fields,
):
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="查询一个工具不支持的字段",
    )
    state["intent"] = {
        "intent": "tool_action",
        "risk_level": "low",
        "candidate_tool": "mcp.market.stock.market_snapshot",
    }

    result = build_planner_agent_node(
        llm_client=InvalidFieldPlanningLlmClient(
            required_fields=required_fields,
            optional_fields=optional_fields,
        ),
        available_tools=[_market_snapshot_tool_spec()],
    )(state)

    assert result["plan"]["execution_mode"] == "direct_response"
    assert result["plan"]["requested_tools"] == []
    assert result["plan"]["risk_controls"]["needs_clarification"] is True
    assert "tool_arguments_invalid" in result["plan"]["risk_controls"]["reason_codes"]


def test_planner_turns_tool_call_request_validation_error_into_clarification():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="执行需要连接凭证的工具",
    )
    state["intent"] = {
        "intent": "tool_action",
        "risk_level": "high",
        "candidate_tool": "mcp.mock.secure_write",
    }
    planner = build_planner_agent_node(
        llm_client=InlineCredentialPlanningLlmClient(),
        available_tools=[
            ToolSpec(
                name="mcp.mock.secure_write",
                version="1.0.0",
                description="测试工具",
                input_schema={"type": "object"},
                risk_level="high",
            )
        ],
    )

    result = planner(state)

    assert result["plan"]["execution_mode"] == "direct_response"
    assert result["plan"]["requested_tools"] == []
    assert result["plan"]["risk_controls"]["needs_clarification"] is True
    assert "tool_arguments_invalid" in result["plan"]["risk_controls"]["reason_codes"]


def test_planner_turns_invalid_tool_json_schema_into_clarification():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="执行配置异常的工具",
    )
    state["intent"] = {
        "intent": "tool_action",
        "risk_level": "low",
        "candidate_tool": "mcp.mock.invalid_tool",
    }
    planner = build_planner_agent_node(
        llm_client=InvalidSchemaToolPlanningLlmClient(),
        available_tools=[
            ToolSpec(
                name="mcp.mock.invalid_tool",
                version="1.0.0",
                description="测试无效 schema",
                input_schema={"type": "invalid-json-schema-type"},
            )
        ],
    )

    result = planner(state)

    assert result["plan"]["execution_mode"] == "direct_response"
    assert result["plan"]["requested_tools"] == []
    assert result["plan"]["risk_controls"]["needs_clarification"] is True
    assert "tool_arguments_invalid" in result["plan"]["risk_controls"]["reason_codes"]


def test_planner_rejects_server_injected_tool_fields_before_plan_enters_state():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="执行服务端工具",
    )
    state["intent"] = {
        "intent": "tool_action",
        "risk_level": "low",
        "candidate_tool": "mcp.mock.server_injected",
    }
    planner = build_planner_agent_node(
        llm_client=ServerInjectedPlanningLlmClient(),
        available_tools=[
            ToolSpec(
                name="mcp.mock.server_injected",
                version="1.0.0",
                description="测试服务端注入参数",
                input_schema={
                    "type": "object",
                    "properties": {"runtime_handle": {"type": "string"}},
                },
                server_injected_fields=["runtime_handle"],
            )
        ],
    )

    with pytest.raises(InlineCredentialsForbiddenError) as exc_info:
        planner(state)

    assert exc_info.value.code == "inline_credentials_forbidden"
    assert state["plan"] is None


def test_planner_agent_builds_skill_execution_plan_for_a_share_task_workflow():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下贵州茅台",
    )
    state["intent"] = {
        "intent": "task_workflow",
        "domain": "a_share_equity_research",
        "sub_intent": "company_snapshot",
        "candidate_skill": "company_snapshot",
        "risk_level": "medium",
        "needs_clarification": False,
        "confidence": 0.88,
        "reason_codes": ["business_profile:company_research.company_snapshot"],
        "next_node": "planner_agent",
    }

    result = build_planner_agent_node(available_tools=[])(state)

    assert result["plan"]["execution_mode"] == "skill_execution"
    assert result["plan"]["selected_skill"]["name"] == "company_snapshot"
    assert result["plan"]["response_policy"]["response_type"] == "equity_research_company_snapshot"
    assert result["plan"]["retrieval"]["required"] is True


def test_planner_builds_dynamic_equity_research_evidence_plan_with_mcp_and_rag():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="结合知识库研报分析一下银之杰",
    )
    state["intent"] = {
        "intent": "task_workflow",
        "domain": "a_share_equity_research",
        "sub_intent": "company_snapshot",
        "candidate_skill": "company_snapshot",
        "risk_level": "medium",
        "needs_clarification": False,
        "confidence": 0.88,
        "reason_codes": ["business_profile:company_research.company_snapshot"],
        "next_node": "planner_agent",
    }
    tools = [
        _stock_tool_spec("mcp.local.stock.resolve_company"),
        _stock_tool_spec("mcp.local.stock.company_profile"),
        _stock_tool_spec("mcp.local.stock.market_snapshot"),
        _stock_tool_spec("mcp.local.stock.price_trend"),
        _stock_tool_spec("mcp.local.stock.financial_metrics"),
        _stock_tool_spec("mcp.local.stock.announcement_search"),
        ToolSpec(
            name="rag_search",
            version="1.0.0",
            description="检索知识库",
            input_schema={"type": "object"},
        ),
    ]

    result = build_planner_agent_node(
        llm_client=EquityResearchPlanningLlmClient(),
        available_tools=tools,
    )(state)

    plan = result["plan"]
    request_names = [request.tool_name for request in plan["requested_tools"]]
    assert plan["execution_mode"] == "tool_execution"
    assert plan["response_policy"]["response_type"] == "equity_research_report"
    assert plan["analysis_dimensions"] == [
        "business",
        "market",
        "valuation",
        "financial",
        "risk",
    ]
    assert request_names == [
        "mcp.local.stock.resolve_company",
        "mcp.local.stock.company_profile",
        "mcp.local.stock.price_trend",
        "mcp.local.stock.market_snapshot",
        "mcp.local.stock.financial_metrics",
        "mcp.local.stock.announcement_search",
        "rag_search",
    ]
    assert plan["requested_tools"][0].arguments["query"] == "银之杰"
    assert plan["requested_tools"][1].arguments["required_fields"] == ["name"]
    assert plan["requested_tools"][1].arguments["optional_fields"] == [
        "industry",
        "main_business",
        "board",
        "registered_address",
    ]
    assert plan["requested_tools"][2].arguments["required_fields"] == ["latest_price"]
    assert plan["requested_tools"][4].arguments["period"] == "latest"
    assert plan["requested_tools"][4].arguments["required_fields"] == []
    assert {
        "revenue_yoy",
        "net_profit_yoy",
        "operating_cash_flow_yoy",
    } <= set(plan["requested_tools"][4].arguments["optional_fields"])
    assert plan["requested_tools"][-1].arguments["query"] == (
        "结合知识库研报分析一下银之杰"
    )


def test_planner_uses_fast_standard_plan_for_broad_company_snapshot():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下长鑫存储",
    )
    state["intent"] = {
        "intent": "task_workflow",
        "domain": "a_share_equity_research",
        "sub_intent": "company_snapshot",
        "candidate_skill": "company_snapshot",
        "risk_level": "medium",
        "confidence": 0.88,
    }
    tools = [
        _stock_tool_spec("mcp.local.stock.resolve_company"),
        _stock_tool_spec("mcp.local.stock.company_profile"),
        _stock_tool_spec("mcp.local.stock.market_snapshot"),
        _stock_tool_spec("mcp.local.stock.price_trend"),
        _stock_tool_spec("mcp.local.stock.financial_metrics"),
        _stock_tool_spec("mcp.local.stock.announcement_search"),
    ]
    llm_client = RecordingLlmClient("should not be called")

    result = build_planner_agent_node(
        llm_client=llm_client,
        available_tools=tools,
    )(state)

    assert llm_client.calls == []
    assert result["plan"]["analysis_dimensions"] == [
        "business",
        "market",
        "valuation",
        "financial",
        "risk",
    ]
    assert result["plan"]["retrieval"]["required"] is False


def test_planner_uses_requested_financial_year_instead_of_latest():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析银之杰 2025 年报",
    )
    state["intent"] = {
        "intent": "task_workflow",
        "domain": "a_share_equity_research",
        "sub_intent": "financial_report_analysis",
        "candidate_skill": "financial_report_analysis",
        "risk_level": "medium",
        "confidence": 0.9,
    }
    tools = [
        _stock_tool_spec("mcp.local.stock.resolve_company"),
        _stock_tool_spec("mcp.local.stock.financial_metrics"),
    ]

    result = build_planner_agent_node(
        llm_client=EquityResearchPlanningLlmClient(),
        available_tools=tools,
    )(state)

    financial_request = next(
        request
        for request in result["plan"]["requested_tools"]
        if request.tool_name.endswith(".financial_metrics")
    )
    assert financial_request.arguments["period"] == "2025"


def test_planner_does_not_force_rag_for_plain_company_analysis_when_decomposition_fails():
    class FailingPlanningClient:
        async def complete(self, **kwargs):
            raise RuntimeError("planner unavailable")

    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下海能达",
    )
    state["intent"] = {
        "intent": "task_workflow",
        "domain": "a_share_equity_research",
        "sub_intent": "company_snapshot",
        "candidate_skill": "company_snapshot",
        "risk_level": "medium",
        "confidence": 0.9,
    }
    tools = [
        _stock_tool_spec("mcp.local.stock.resolve_company"),
        _stock_tool_spec("mcp.local.stock.company_profile"),
        ToolSpec(
            name="rag_search",
            version="1.0.0",
            description="检索知识库",
            input_schema={"type": "object"},
        ),
    ]

    result = build_planner_agent_node(
        llm_client=FailingPlanningClient(),
        available_tools=tools,
    )(state)

    assert result["plan"]["retrieval"]["required"] is False
    assert all(
        request.tool_name != "rag_search"
        for request in result["plan"]["requested_tools"]
    )


def test_planner_agent_does_not_apply_equity_plan_to_non_financial_candidate_skill():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="把客户状态改成已跟进",
    )
    state["intent"] = {
        "intent": "task_workflow",
        "domain": "crm",
        "sub_intent": "update_customer_status",
        "candidate_skill": "crm_update",
        "risk_level": "medium",
        "needs_clarification": False,
        "confidence": 0.84,
        "reason_codes": ["classifier_selected_crm_update"],
        "next_node": "planner_agent",
    }

    result = build_planner_agent_node(available_tools=[])(state)

    assert result["plan"]["execution_mode"] == "direct_response"
    assert result["plan"]["response_policy"]["response_type"] == "workflow_not_ready_response"
    assert "disclaimer_required" not in result["plan"]["response_policy"]


def test_skill_agent_selects_and_runs_registered_skill_with_runtime_context():
    skill = RecordingSkill()
    registry = SkillRegistry()
    registry.register(skill)
    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
    )
    state["intent"] = {
        "intent": "knowledge_qa",
        "candidate_skill": "recording_skill",
        "risk_level": "low",
        "needs_clarification": False,
        "reason_codes": ["policy_question"],
        "next_node": "skill_agent",
    }
    state["image_analyses"] = [
        {
            "asset_id": "asset-1",
            "analysis_id": "analysis-1",
            "media_type": "image/png",
            "width": 100,
            "height": 80,
            "status": "ready",
            "document_type": "financial_statement",
            "summary": "贵州茅台营业收入增长。",
            "extracted_text": "不应传入 Skill 的 OCR 正文",
            "financial_metrics": [],
            "chart_observations": [],
            "entities": ["贵州茅台", "600519"],
            "risk_flags": [],
            "confidence": 0.9,
            "truncated": False,
        }
    ]
    state["plan"] = {
        "selected_skill": {"name": "recording_skill"},
        "execution_steps": ["select_skill", "retrieve_evidence"],
    }

    result = build_skill_agent_node(registry=registry)(state)

    assert result["selected_skill"]["skill_name"] == "recording_skill"
    assert result["selected_skill"]["skill_version"] == "2.0.0"
    assert result["selected_skill"]["status"] == "needs_retrieval"
    assert result["selected_skill"]["allowed_tools"] == ["rag_search"]
    assert result["plan"]["execution_steps"] == ["select_skill", "retrieve_evidence"]
    assert result["plan"]["skill_output"]["skill_name"] == "recording_skill"
    assert result["plan"]["requested_tools"][0].tool_name == "rag_search"
    assert "memory_context" not in skill.calls[0]
    assert skill.calls[0]["context"]["image_analyses"] == [
        {
            "analysis_id": "analysis-1",
            "document_type": "financial_statement",
            "summary": "贵州茅台营业收入增长。",
            "entities": ["贵州茅台", "600519"],
            "warnings": [],
            "confidence": 0.9,
        }
    ]
    assert "OCR 正文" not in str(skill.calls[0]["context"])


def test_skill_runtime_rejects_bypassed_inline_credentials_before_plan_update():
    class CredentialSkill(Skill):
        spec = SkillSpec(
            name="credential_skill",
            version="1.0.0",
            description="验证 Skill 凭证边界",
            supported_intents=["task_workflow"],
            risk_level="high",
            allowed_tools=["mcp.mock.secure_write"],
        )

        def run(self, **kwargs):
            return SkillOutput(
                skill_name=self.spec.name,
                skill_version=self.spec.version,
                status="needs_approval",
                requested_tools=[
                    ToolCallRequest.model_construct(
                        tool_name="mcp.mock.secure_write",
                        arguments={"database_password": "plaintext-secret"},
                        purpose="尝试绕过 Pydantic 校验",
                    )
                ],
            )

    registry = SkillRegistry()
    registry.register(CredentialSkill())
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="执行安全写入",
    )
    state["intent"] = {
        "intent": "task_workflow",
        "candidate_skill": "credential_skill",
        "risk_level": "high",
    }
    state["plan"] = {"selected_skill": {"name": "credential_skill"}}

    with pytest.raises(InlineCredentialsForbiddenError) as exc_info:
        build_skill_agent_node(registry=registry)(state)

    assert exc_info.value.code == "inline_credentials_forbidden"
    assert "requested_tools" not in state["plan"]


def test_tool_agent_rejects_server_injected_schema_fields_before_provider_call():
    class InjectedInput(BaseModel):
        runtime_handle: str

    provider_calls = []

    class InjectedTool(Tool[InjectedInput]):
        name = "mcp.mock.injected"
        version = "1.0.0"
        input_model = InjectedInput
        server_injected_fields = ["runtime_handle"]

        async def run(self, input_data: InjectedInput) -> ToolCallResult:
            provider_calls.append(input_data.runtime_handle)
            raise AssertionError("服务端注入字段不得来自 Agent State")

    def registry_factory():
        registry = ToolRegistry()
        registry.register(InjectedTool())
        return registry

    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="执行服务端工具",
    )
    state["plan"] = {
        "tool_policy": {"allowed_tools": ["mcp.mock.injected"]},
        "requested_tools": [
            ToolCallRequest(
                tool_name="mcp.mock.injected",
                arguments={"runtime_handle": "model-supplied-value"},
                purpose="验证 schema 驱动二次拦截",
            )
        ],
    }
    state["tool_catalog_digest"] = registry_factory().catalog_digest

    with pytest.raises(InlineCredentialsForbiddenError):
        build_tool_agent_node(registry_factory=registry_factory)(state)

    assert provider_calls == []


def test_tool_agent_preserves_sanitized_error_details_and_retryability():
    class FailingInput(BaseModel):
        symbol: str

    class FailingTool(Tool[FailingInput]):
        name = "mcp.production.stock.market_snapshot"
        version = "1.0.0"
        input_model = FailingInput

        async def run(self, input_data: FailingInput) -> ToolCallResult:
            return ToolCallResult(
                tool_call_id="untrusted-call-id",
                tool_name=self.name,
                status="failed",
                error=ToolError(
                    code="provider_rate_limited",
                    message="市场数据服务调用失败",
                    details={
                        "retry_after_seconds": 30,
                        "token": "super-secret-value",
                    },
                    retryable=True,
                ),
                trace_id="untrusted-trace-id",
            )

    def registry_factory():
        registry = ToolRegistry()
        registry.register(FailingTool())
        return registry

    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="查询行情",
    )
    state["plan"] = {
        "tool_policy": {
            "allowed_tools": ["mcp.production.stock.market_snapshot"],
        },
        "requested_tools": [
            ToolCallRequest(
                tool_name="mcp.production.stock.market_snapshot",
                arguments={"symbol": "300085.SZ"},
                purpose="获取行情快照",
            )
        ],
    }
    state["tool_catalog_digest"] = registry_factory().catalog_digest

    result = build_tool_agent_node(registry_factory=registry_factory)(state)

    assert result["errors"][-1]["details"] == {
        "retry_after_seconds": 30,
        "token": "***",
    }
    assert result["errors"][-1]["retryable"] is True


def test_tool_agent_does_not_promote_optional_market_data_failure_to_state_error():
    class AnnouncementInput(BaseModel):
        symbol: str
        required_fields: list[str]

    class AnnouncementTool(Tool[AnnouncementInput]):
        name = "mcp.mock.stock.announcement_search"
        version = "1.0.0"
        input_model = AnnouncementInput

        async def run(self, input_data: AnnouncementInput) -> ToolCallResult:
            return ToolCallResult(
                tool_call_id="tool_announcement",
                tool_name=self.name,
                status="failed",
                error=ToolError(
                    code="provider_permission_denied",
                    message="公告接口无权限",
                    details={},
                    retryable=False,
                ),
                trace_id="trace_tool",
            )

    def registry_factory():
        registry = ToolRegistry()
        registry.register(AnnouncementTool())
        return registry

    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析公告",
    )
    state["plan"] = {
        "tool_policy": {
            "allowed_tools": ["mcp.mock.stock.announcement_search"],
        },
        "requested_tools": [
            ToolCallRequest(
                tool_name="mcp.mock.stock.announcement_search",
                arguments={"symbol": "300085.SZ", "required_fields": []},
                purpose="获取公告增强信息",
            )
        ],
    }
    state["tool_catalog_digest"] = registry_factory().catalog_digest

    result = build_tool_agent_node(registry_factory=registry_factory)(state)

    assert result["tool_calls"][0].result.status == "failed"
    assert result["errors"] == []


def test_tool_agent_continues_after_optional_market_data_failure():
    class FinancialInput(BaseModel):
        symbol: str
        required_fields: list[str]

    class AnnouncementInput(BaseModel):
        symbol: str
        required_fields: list[str]

    class FinancialTool(Tool[FinancialInput]):
        name = "mcp.mock.stock.financial_metrics"
        version = "1.0.0"
        input_model = FinancialInput

        async def run(self, input_data: FinancialInput) -> ToolCallResult:
            return ToolCallResult(
                tool_call_id="tool_financial",
                tool_name=self.name,
                status="failed",
                error=ToolError(
                    code="required_api_permission_missing",
                    message="财务接口无权限",
                    details={},
                    retryable=False,
                ),
                trace_id="trace_tool",
            )

    class AnnouncementTool(Tool[AnnouncementInput]):
        name = "mcp.mock.stock.announcement_search"
        version = "1.0.0"
        input_model = AnnouncementInput

        async def run(self, input_data: AnnouncementInput) -> ToolCallResult:
            return ToolCallResult(
                tool_call_id="tool_announcement",
                tool_name=self.name,
                status="success",
                result={
                    "status": "complete",
                    "data": {"announcements": []},
                    "coverage": {
                        "status": "complete",
                        "requested_fields": ["announcements"],
                        "provided_fields": ["announcements"],
                        "missing_optional_fields": [],
                        "source_apis": ["anns"],
                    },
                },
                trace_id="trace_tool",
            )

    def registry_factory():
        registry = ToolRegistry()
        registry.register(FinancialTool())
        registry.register(AnnouncementTool())
        return registry

    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析公司",
    )
    state["plan"] = {
        "tool_policy": {
            "allowed_tools": [
                "mcp.mock.stock.financial_metrics",
                "mcp.mock.stock.announcement_search",
            ],
        },
        "requested_tools": [
            ToolCallRequest(
                tool_name="mcp.mock.stock.financial_metrics",
                arguments={"symbol": "300085.SZ", "required_fields": []},
                purpose="获取可选财务增强信息",
            ),
            ToolCallRequest(
                tool_name="mcp.mock.stock.announcement_search",
                arguments={
                    "symbol": "300085.SZ",
                    "required_fields": ["announcements"],
                },
                purpose="继续获取公告证据",
            ),
        ],
    }
    state["tool_catalog_digest"] = registry_factory().catalog_digest

    result = build_tool_agent_node(registry_factory=registry_factory)(state)

    assert [record.result.status for record in result["tool_calls"]] == [
        "failed",
        "success",
    ]
    assert result["errors"] == []


def test_tool_agent_replaces_resolved_symbol_placeholder_in_later_stock_tools():
    class ResolveInput(BaseModel):
        query: str

    class ProfileInput(BaseModel):
        symbol: str

    class ResolveCompanyTool(Tool[ResolveInput]):
        name = "mcp.mock.stock.resolve_company"
        version = "1.0.0"
        input_model = ResolveInput

        async def run(self, input_data: ResolveInput) -> ToolCallResult:
            return ToolCallResult(
                tool_call_id="tool_resolve",
                tool_name=self.name,
                status="success",
                result={"query": input_data.query, "symbol": "600519", "name": "贵州茅台"},
                trace_id="trace_tool",
            )

    class CompanyProfileTool(Tool[ProfileInput]):
        name = "mcp.mock.stock.company_profile"
        version = "1.0.0"
        input_model = ProfileInput

        async def run(self, input_data: ProfileInput) -> ToolCallResult:
            return ToolCallResult(
                tool_call_id="tool_profile",
                tool_name=self.name,
                status="success",
                result={"symbol": input_data.symbol, "name": "贵州茅台"},
                trace_id="trace_tool",
            )

    def registry_factory() -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(ResolveCompanyTool())
        registry.register(CompanyProfileTool())
        return registry

    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下贵州茅台",
    )
    state["plan"] = {
        "tool_policy": {
            "allowed_tools": [
                "mcp.mock.stock.resolve_company",
                "mcp.mock.stock.company_profile",
            ]
        },
        "requested_tools": [
            ToolCallRequest(
                tool_name="mcp.mock.stock.resolve_company",
                arguments={"query": "贵州茅台"},
                purpose="解析公司简称",
            ),
            ToolCallRequest(
                tool_name="mcp.mock.stock.company_profile",
                arguments={"symbol": "$resolved_symbol"},
                purpose="查询公司资料",
            ),
        ]
    }
    state["tool_catalog_digest"] = registry_factory().catalog_digest

    result = build_tool_agent_node(registry_factory=registry_factory)(state)

    assert result["tool_calls"][1].request.arguments["symbol"] == "600519"
    assert result["tool_calls"][1].result.result["symbol"] == "600519"


def test_tool_agent_replaces_placeholder_from_market_data_envelope():
    profile_calls: list[str] = []

    class ResolveInput(BaseModel):
        query: str

    class ProfileInput(BaseModel):
        symbol: str

    class ResolveCompanyTool(Tool[ResolveInput]):
        name = "mcp.mock.stock.resolve_company"
        version = "1.0.0"
        input_model = ResolveInput

        async def run(self, input_data: ResolveInput) -> ToolCallResult:
            return ToolCallResult(
                tool_call_id="tool_resolve",
                tool_name=self.name,
                status="success",
                result={
                    "status": "complete",
                    "data": {
                        "symbol": "300085.SZ",
                        "name": "银之杰",
                        "exchange": "SZSE",
                    },
                    "coverage": {
                        "status": "complete",
                        "requested_fields": ["symbol", "name", "exchange"],
                        "provided_fields": ["symbol", "name", "exchange"],
                        "missing_optional_fields": [],
                        "source_apis": ["stock_basic"],
                    },
                },
                trace_id="trace_tool",
            )

    class CompanyProfileTool(Tool[ProfileInput]):
        name = "mcp.mock.stock.company_profile"
        version = "1.0.0"
        input_model = ProfileInput

        async def run(self, input_data: ProfileInput) -> ToolCallResult:
            profile_calls.append(input_data.symbol)
            return ToolCallResult(
                tool_call_id="tool_profile",
                tool_name=self.name,
                status="success",
                result={
                    "status": "complete",
                    "data": {"symbol": input_data.symbol, "name": "银之杰"},
                    "coverage": {
                        "status": "complete",
                        "requested_fields": ["name"],
                        "provided_fields": ["name"],
                        "missing_optional_fields": [],
                        "source_apis": ["stock_basic"],
                    },
                },
                trace_id="trace_tool",
            )

    def registry_factory() -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(ResolveCompanyTool())
        registry.register(CompanyProfileTool())
        return registry

    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
    )
    state["plan"] = {
        "tool_policy": {
            "allowed_tools": [
                "mcp.mock.stock.resolve_company",
                "mcp.mock.stock.company_profile",
            ]
        },
        "requested_tools": [
            ToolCallRequest(
                tool_name="mcp.mock.stock.resolve_company",
                arguments={"query": "银之杰"},
                purpose="解析公司简称",
            ),
            ToolCallRequest(
                tool_name="mcp.mock.stock.company_profile",
                arguments={"symbol": "$resolved_symbol"},
                purpose="查询公司资料",
            ),
        ],
    }
    state["tool_catalog_digest"] = registry_factory().catalog_digest

    result = build_tool_agent_node(registry_factory=registry_factory)(state)

    assert profile_calls == ["300085.SZ"]
    assert result["tool_calls"][1].request.arguments["symbol"] == "300085.SZ"
    assert not [
        error for error in result["errors"] if error["code"] == "company_not_found"
    ]


def test_tool_agent_stops_dependent_calls_when_company_resolution_is_empty():
    profile_calls: list[str] = []

    class ResolveInput(BaseModel):
        query: str

    class ProfileInput(BaseModel):
        symbol: str

    class ResolveCompanyTool(Tool[ResolveInput]):
        name = "mcp.mock.stock.resolve_company"
        version = "1.0.0"
        input_model = ResolveInput

        async def run(self, input_data: ResolveInput) -> ToolCallResult:
            return ToolCallResult(
                tool_call_id="tool_resolve",
                tool_name=self.name,
                status="success",
                result={
                    "query": input_data.query,
                    "symbol": "",
                    "name": "",
                    "warnings": ["fixture_company_not_found"],
                },
                trace_id="trace_tool",
            )

    class CompanyProfileTool(Tool[ProfileInput]):
        name = "mcp.mock.stock.company_profile"
        version = "1.0.0"
        input_model = ProfileInput

        async def run(self, input_data: ProfileInput) -> ToolCallResult:
            profile_calls.append(input_data.symbol)
            raise AssertionError("公司解析失败后不得执行依赖工具")

    def registry_factory() -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(ResolveCompanyTool())
        registry.register(CompanyProfileTool())
        return registry

    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="帮我分析下银之杰",
    )
    state["plan"] = {
        "tool_policy": {
            "allowed_tools": [
                "mcp.mock.stock.resolve_company",
                "mcp.mock.stock.company_profile",
            ]
        },
        "requested_tools": [
            ToolCallRequest(
                tool_name="mcp.mock.stock.resolve_company",
                arguments={"query": "银之杰"},
                purpose="解析公司简称",
            ),
            ToolCallRequest(
                tool_name="mcp.mock.stock.company_profile",
                arguments={"symbol": "$resolved_symbol"},
                purpose="查询公司资料",
            ),
        ],
    }
    state["tool_catalog_digest"] = registry_factory().catalog_digest

    result = build_tool_agent_node(registry_factory=registry_factory)(state)

    assert len(result["tool_calls"]) == 1
    assert profile_calls == []
    assert result["errors"][-1] == {
        "node": "tool_agent",
        "message": "当前市场数据源未覆盖该公司，无法生成可靠投研分析。",
        "code": "company_not_found",
        "http_status": 422,
        "handled": True,
    }


def test_tool_agent_node_enforces_plan_tool_policy():
    class EchoInput(BaseModel):
        text: str

    class EchoTool(Tool[EchoInput]):
        name = "echo"
        version = "1.0.0"
        input_model = EchoInput

        async def run(self, input_data: EchoInput) -> ToolCallResult:
            return ToolCallResult(
                tool_call_id="tool_echo",
                tool_name=self.name,
                status="success",
                result={"text": input_data.text},
                trace_id="trace_tool",
            )

    def registry_factory() -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(EchoTool())
        return registry

    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="echo hello",
    )
    state["plan"] = {
        "tool_policy": {"allowed_tools": ["another_tool"]},
        "requested_tools": [
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证节点执行策略",
            )
        ],
    }
    state["tool_catalog_digest"] = registry_factory().catalog_digest

    result = build_tool_agent_node(registry_factory=registry_factory)(state)

    assert result["tool_calls"][0].result.status == "denied"
    assert result["tool_calls"][0].result.error.code == "tool_not_allowed_by_plan"


def test_configured_skill_requests_admin_allowed_stock_tools_and_rag_search():
    skill = _configured_skill(
        allowed_tools=[
            "mcp.mock.stock.resolve_company",
            "mcp.mock.stock.company_profile",
            "mcp.mock.stock.market_snapshot",
            "mcp.mock.stock.announcement_search",
            "rag_search",
        ]
    )

    output = skill.run_validated(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下贵州茅台",
        context={"intent": {"sub_intent": "company_snapshot"}},
        evidence=None,
    )
    request_names = [request.tool_name for request in output.requested_tools]

    assert output.skill_name == "company_snapshot"
    assert "mcp.mock.stock.resolve_company" in request_names
    assert "mcp.mock.stock.company_profile" in request_names
    assert "mcp.mock.stock.market_snapshot" in request_names
    assert "rag_search" in request_names
    assert output.requested_tools[1].arguments["symbol"] == "$resolved_symbol"
    assert output.requested_tools[0].arguments["required_fields"] == ["symbol"]
    assert output.requested_tools[1].arguments["required_fields"] == ["name"]
    assert output.requested_tools[2].arguments["required_fields"] == ["price"]
    assert output.requested_tools[3].arguments["required_fields"] == [
        "announcements"
    ]


def test_configured_skill_extracts_company_query_from_technical_indicator_request():
    skill = _configured_skill(
        allowed_tools=["mcp.mock.stock.resolve_company"],
        evidence_contract={
            "requirements": [
                {
                    "capability": "stock.resolve_company",
                    "required_fields": ["symbol", "name"],
                    "optional_fields": ["exchange"],
                }
            ]
        },
    )

    output = skill.run_validated(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰技术指标",
        context={"intent": {"sub_intent": "company_snapshot"}},
        evidence=None,
    )

    assert output.requested_tools[0].arguments["query"] == "银之杰"


def test_configured_skill_uses_image_entities_for_company_resolution():
    skill = _configured_skill(
        allowed_tools=["mcp.mock.stock.resolve_company"]
    )

    output = skill.run_validated(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析这张图",
        context={
            "intent": {"sub_intent": "financial_report_analysis"},
            "image_analyses": [
                {
                    "analysis_id": "analysis-1",
                    "document_type": "financial_statement",
                    "summary": "贵州茅台财报摘要",
                    "entities": ["贵州茅台", "600519"],
                    "warnings": [],
                    "confidence": 0.9,
                }
            ],
        },
        evidence=None,
    )

    assert output.requested_tools[0].arguments["query"] == "600519"


def test_configured_skill_requires_approval_for_high_risk_definition():
    skill = _configured_skill(
        allowed_tools=["rag_search"],
        risk_level="high",
    )

    assert skill.spec.approval_policy.require_human is True
    output = skill.run_validated(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析宁德时代最近有什么风险",
        context={"intent": {"sub_intent": "risk_scan"}},
        evidence=None,
    )
    assert output.requested_tools[0].approval_required is True


def test_configured_skill_parses_financial_report_periods():
    skill = _configured_skill(
        allowed_tools=[
            "mcp.mock.stock.resolve_company",
            "mcp.mock.stock.financial_metrics",
        ],
    )

    annual_output = skill.run_validated(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析宁德时代 2024 年报",
        context={"intent": {"sub_intent": "financial_report_analysis"}},
        evidence=None,
    )
    half_year_output = skill.run_validated(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析宁德时代 2024 半年报",
        context={"intent": {"sub_intent": "financial_report_analysis"}},
        evidence=None,
    )

    assert annual_output.requested_tools[1].arguments["period"] == "2024"
    assert half_year_output.requested_tools[1].arguments["period"] == "2024H1"


def test_configured_skill_matches_market_capability_with_arbitrary_mcp_server_name():
    skill = _configured_skill(
        allowed_tools=[
            "mcp.production-tushare.stock.resolve_company",
            "mcp.production-tushare.stock.financial_metrics",
        ],
        evidence_contract={
            "requirements": [
                {
                    "capability": "stock.resolve_company",
                    "required_fields": ["symbol", "name"],
                    "optional_fields": ["exchange"],
                },
                {
                    "capability": "stock.financial_metrics",
                    "required_fields": ["revenue", "net_profit_parent"],
                    "optional_fields": ["roe"],
                },
            ]
        },
    )

    output = skill.run_validated(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析宁德时代 2024 年报",
        context={"intent": {"sub_intent": "financial_report_analysis"}},
        evidence=None,
    )

    resolver, financial = output.requested_tools
    assert resolver.arguments == {
        "query": "宁德时代",
        "required_fields": ["symbol", "name"],
        "optional_fields": ["exchange"],
    }
    assert financial.arguments == {
        "symbol": "$resolved_symbol",
        "period": "2024",
        "required_fields": ["revenue", "net_profit_parent"],
        "optional_fields": ["roe"],
    }


@pytest.mark.parametrize(
    ("evidence_contract", "reason_code"),
    [
        ({"requirements": []}, "skill_evidence_contract_missing"),
        (
            {
                "requirements": [
                    {
                        "capability": "stock.resolve_company",
                        "required_fields": ["symbol"],
                        "optional_fields": [],
                    }
                ]
            },
            "skill_evidence_contract_missing",
        ),
    ],
)
def test_configured_skill_refuses_all_tools_when_market_evidence_contract_is_missing(
    evidence_contract,
    reason_code,
):
    skill = _configured_skill(
        allowed_tools=[
            "mcp.vendor.stock.resolve_company",
            "mcp.vendor.stock.market_snapshot",
            "rag_search",
        ],
        evidence_contract=evidence_contract,
    )

    output = skill.run_validated(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析贵州茅台",
        context={"intent": {"sub_intent": "company_snapshot"}},
        evidence=None,
    )

    assert output.status == "failed"
    assert output.requested_tools == []
    assert output.reason_codes == [reason_code]


def test_configured_skill_refuses_invalid_admin_evidence_contract():
    skill = _configured_skill(
        allowed_tools=["mcp.vendor.stock.resolve_company", "rag_search"],
        evidence_contract={"requirements": []},
        evidence_contract_error="skill_evidence_contract_invalid",
    )

    output = skill.run_validated(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析贵州茅台",
        context={"intent": {"sub_intent": "company_snapshot"}},
        evidence=None,
    )

    assert output.status == "failed"
    assert output.requested_tools == []
    assert output.reason_codes == ["skill_evidence_contract_invalid"]


def test_configured_skill_rejects_contract_capability_not_present_in_allowed_tools():
    skill = _configured_skill(
        allowed_tools=["mcp.vendor.stock.resolve_company", "rag_search"],
        evidence_contract={
            "requirements": [
                {
                    "capability": "stock.resolve_company",
                    "required_fields": ["symbol"],
                    "optional_fields": [],
                },
                {
                    "capability": "stock.market_snapshot",
                    "required_fields": ["price"],
                    "optional_fields": [],
                },
            ]
        },
    )

    output = skill.run_validated(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析贵州茅台",
        context={"intent": {"sub_intent": "company_snapshot"}},
        evidence=None,
    )

    assert output.status == "failed"
    assert output.requested_tools == []
    assert output.reason_codes == ["skill_evidence_contract_invalid"]


@pytest.mark.parametrize(
    ("evidence_contract", "evidence_contract_error", "code", "message"),
    [
        (
            {"requirements": []},
            None,
            "skill_evidence_contract_missing",
            "Skill 证据契约未配置，请联系管理员完善配置。",
        ),
        (
            {"requirements": []},
            "skill_evidence_contract_invalid",
            "skill_evidence_contract_invalid",
            "Skill 证据契约无效，请联系管理员检查配置。",
        ),
    ],
)
def test_skill_agent_turns_evidence_contract_failure_into_safe_handled_error(
    evidence_contract,
    evidence_contract_error,
    code,
    message,
):
    registry = SkillRegistry()
    registry.register(
        _configured_skill(
            allowed_tools=["mcp.vendor.stock.resolve_company", "rag_search"],
            evidence_contract=evidence_contract,
            evidence_contract_error=evidence_contract_error,
        )
    )
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析贵州茅台",
    )
    state["intent"] = {
        "intent": "task_workflow",
        "domain": "a_share_equity_research",
        "candidate_skill": "company_snapshot",
    }
    state["plan"] = {
        "execution_mode": "skill_execution",
        "selected_skill": {"name": "company_snapshot"},
        "response_policy": {
            "response_type": "equity_research_company_snapshot",
        },
    }

    state = build_skill_agent_node(registry=registry)(state)
    state = synthesizer_node(state)

    assert state["errors"][-1] == {
        "node": "skill_agent",
        "message": message,
        "code": code,
        "http_status": 422,
        "handled": True,
    }
    assert state["draft_answer"]["response_type"] == "execution_error_response"
    assert state["draft_answer"]["business_error_code"] == code
    assert message in state["draft_answer"]["content"]
    assert "公司概览" not in state["draft_answer"]["content"]


def test_configured_skill_rejects_symbol_tools_without_resolver_first():
    import pytest

    with pytest.raises(ValueError, match="requires a stock.resolve_company tool"):
        _configured_skill(
            allowed_tools=["mcp.mock.stock.company_profile"],
        )

    with pytest.raises(ValueError, match="requires stock.resolve_company before it"):
        _configured_skill(
            allowed_tools=[
                "mcp.mock.stock.company_profile",
                "mcp.mock.stock.resolve_company",
            ],
        )


def _market_data_envelope(
    *,
    data: dict,
    provided_fields: list[str],
    requested_fields: list[str] | None = None,
    missing_optional_fields: list[str] | None = None,
    source_apis: list[str] | None = None,
    status: str = "complete",
) -> dict:
    requested = requested_fields or provided_fields
    missing_optional = missing_optional_fields or []
    return {
        "status": status,
        "data": data,
        "coverage": {
            "status": status,
            "requested_fields": requested,
            "required_fields": provided_fields,
            "optional_fields": missing_optional,
            "provided_fields": provided_fields,
            "missing_required_fields": [],
            "missing_optional_fields": missing_optional,
            "source_apis": source_apis or ["daily"],
        },
    }


def test_analysis_agent_builds_price_fact_from_production_server_envelope():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下贵州茅台",
    )
    state["tool_calls"] = [
        ToolCallRecord(
            call_id="record_1",
            request=ToolCallRequest(
                tool_name="mcp.production-tushare.stock.market_snapshot",
                arguments={"symbol": "600519.SH"},
                purpose="获取行情快照",
            ),
            result=ToolCallResult(
                tool_call_id="result_1",
                tool_name="mcp.production-tushare.stock.market_snapshot",
                status="success",
                result=_market_data_envelope(
                    data={
                        "symbol": "600519.SH",
                        "price": 1418.2,
                        "source_provider": "tushare",
                        "as_of_date": "2026-07-26",
                        "row_count": 1,
                    },
                    requested_fields=["price"],
                    provided_fields=["price"],
                    source_apis=["daily"],
                ),
                trace_id="trace_1",
            ),
        )
    ]

    result = analysis_agent_node(state)

    assert result["analysis"]["structured_tool_results"][0]["result"]["price"] == 1418.2
    assert result["analysis"]["tool_facts"] == [
        {
            "capability": "stock.market_snapshot",
            "field_name": "price",
            "value": 1418.2,
            "ref": {
                "tool_call_id": "record_1",
                "field_path": "data.price",
                "value": 1418.2,
            },
        }
    ]
    assert result["analysis"]["data_coverage"] == [
        {
            "tool_call_id": "record_1",
                "capability": "stock.market_snapshot",
                "status": "complete",
                "requested_fields": ["price"],
                "provided_fields": ["price"],
            "missing_optional_fields": [],
            "source_apis": ["daily"],
        }
    ]


def test_analysis_agent_builds_financial_metric_facts_from_metrics_object():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析财务指标",
    )
    state["tool_calls"] = [
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.vendor-a.stock.financial_metrics",
                arguments={"symbol": "300085.SZ", "period": "2025"},
                purpose="获取财务指标",
            ),
            result=ToolCallResult(
                tool_call_id="financial_1",
                tool_name="mcp.vendor-a.stock.financial_metrics",
                status="success",
                result=_market_data_envelope(
                    data={
                        "symbol": "300085.SZ",
                        "period": "2025",
                        "metrics": {"revenue": 123.4, "net_profit": 8.6},
                        "source_provider": "tushare",
                    },
                    requested_fields=["revenue", "net_profit"],
                    provided_fields=["revenue", "net_profit"],
                    source_apis=["income"],
                ),
                trace_id="trace_1",
            ),
        )
    ]

    facts = analysis_agent_node(state)["analysis"]["tool_facts"]

    assert [(fact["field_name"], fact["value"]) for fact in facts] == [
        ("revenue", 123.4),
        ("net_profit", 8.6),
    ]
    assert [fact["ref"]["field_path"] for fact in facts] == [
        "data.metrics.revenue",
        "data.metrics.net_profit",
    ]


def test_analysis_agent_partial_envelope_only_emits_provided_requested_fields():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="查询价格和估值",
    )
    state["tool_calls"] = [
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.any-server.stock.market_snapshot",
                arguments={"symbol": "300085.SZ"},
                purpose="获取行情快照",
            ),
            result=ToolCallResult(
                tool_call_id="snapshot_1",
                tool_name="mcp.any-server.stock.market_snapshot",
                status="success",
                result=_market_data_envelope(
                    status="partial",
                    data={
                        "price": 10.5,
                        "pe_ttm": 99.9,
                        "internal_count": 7,
                    },
                    requested_fields=["price", "pb"],
                    provided_fields=["price"],
                    missing_optional_fields=["pb"],
                    source_apis=["daily", "daily_basic"],
                ),
                trace_id="trace_1",
            ),
        )
    ]

    analysis = analysis_agent_node(state)["analysis"]

    assert [fact["field_name"] for fact in analysis["tool_facts"]] == ["price"]
    assert analysis["data_coverage"][0]["status"] == "partial"
    assert analysis["data_coverage"][0]["missing_optional_fields"] == ["pb"]


def test_analysis_agent_builds_one_collection_fact_for_announcements():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="查询公告",
    )
    announcements = [
        {"title": "年度报告", "date": "2026-03-31", "url": "https://example.test/1"},
        {"title": "分红公告", "date": "2026-06-01", "url": "https://example.test/2"},
    ]
    state["tool_calls"] = [
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.remote.stock.announcement_search",
                arguments={"symbol": "600519.SH"},
                purpose="获取公告",
            ),
            result=ToolCallResult(
                tool_call_id="announcement_1",
                tool_name="mcp.remote.stock.announcement_search",
                status="success",
                result=_market_data_envelope(
                    data={"announcements": announcements},
                    requested_fields=["announcements"],
                    provided_fields=["announcements"],
                    source_apis=["anns_d"],
                ),
                trace_id="trace_1",
            ),
        )
    ]

    facts = analysis_agent_node(state)["analysis"]["tool_facts"]

    assert facts[0]["field_name"] == "announcements"
    assert facts[0]["value"] == announcements
    assert facts[0]["ref"]["field_path"] == "data.announcements"


def test_analysis_agent_ignores_failed_malformed_and_non_stock_results():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析公司并计算",
    )
    state["tool_calls"] = [
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.vendor.stock.market_snapshot",
                arguments={"symbol": "300085.SZ"},
                purpose="失败调用",
            ),
            result=ToolCallResult(
                tool_call_id="failed_1",
                tool_name="mcp.vendor.stock.market_snapshot",
                status="failed",
                trace_id="trace_1",
            ),
        ),
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.vendor.stock.company_profile",
                arguments={"symbol": "300085.SZ"},
                purpose="畸形响应",
            ),
            result=ToolCallResult(
                tool_call_id="malformed_1",
                tool_name="mcp.vendor.stock.company_profile",
                status="success",
                result={"status": "complete", "data": {"name": "银之杰"}},
                trace_id="trace_1",
            ),
        ),
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.vendor.stock.market_snapshot",
                arguments={"symbol": "300085.SZ"},
                purpose="业务失败响应",
            ),
            result=ToolCallResult(
                tool_call_id="envelope_failed_1",
                tool_name="mcp.vendor.stock.market_snapshot",
                status="success",
                result={
                    "status": "failed",
                    "error": {"code": "provider_unavailable", "token": "secret"},
                },
                trace_id="trace_1",
            ),
        ),
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.vendor.echo",
                arguments={"numbers": [1, 2, 3]},
                purpose="求和",
            ),
            result=ToolCallResult(
                tool_call_id="sum_1",
                tool_name="mcp.vendor.echo",
                status="success",
                result={"total": 6, "count": 3},
                trace_id="trace_1",
            ),
        ),
    ]

    analysis = analysis_agent_node(state)["analysis"]

    assert analysis["tool_facts"] == []
    assert analysis["structured_tool_results"] == []
    assert [item["status"] for item in analysis["data_coverage"]] == [
        "failed",
        "ignored",
        "failed",
    ]
    assert [item["error_code"] for item in analysis["data_coverage"]] == [
        "tool_call_failed",
        "invalid_market_data_envelope",
        "provider_unavailable",
    ]


def test_analysis_agent_bounds_and_redacts_coverage_summary():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="查询行情",
    )
    source_apis = [f"api_{index}" for index in range(40)] + ["token=super-secret-value"]
    state["tool_calls"] = [
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.remote.stock.market_snapshot",
                arguments={"symbol": "300085.SZ"},
                purpose="获取行情",
            ),
            result=ToolCallResult(
                tool_call_id="bounded_1",
                tool_name="mcp.remote.stock.market_snapshot",
                status="success",
                result=_market_data_envelope(
                    data={"price": 10.5},
                    requested_fields=["price"],
                    provided_fields=["price"],
                    source_apis=source_apis,
                ),
                trace_id="trace_1",
            ),
        )
    ]

    coverage = analysis_agent_node(state)["analysis"]["data_coverage"][0]

    assert len(coverage["source_apis"]) <= 33
    assert "super-secret-value" not in json.dumps(coverage, ensure_ascii=False)


def _equity_market_state(
    *,
    data: dict,
    requested_fields: list[str],
    provided_fields: list[str],
    status: str = "complete",
    missing_optional_fields: list[str] | None = None,
    missing_required_fields: list[str] | None = None,
):
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下银之杰",
    )
    state["intent"] = {
        "intent": "task_workflow",
        "domain": "a_share_equity_research",
        "sub_intent": "company_snapshot",
    }
    state["plan"] = {
        "execution_mode": "skill_execution",
        "response_policy": {
            "response_type": "equity_research_company_snapshot",
            "citation_required": False,
            "disclaimer_required": True,
        },
    }
    envelope = _market_data_envelope(
        data=data,
        requested_fields=requested_fields,
        provided_fields=provided_fields,
        status=status,
        missing_optional_fields=missing_optional_fields,
        source_apis=["daily", "daily_basic"],
    )
    envelope["coverage"]["missing_required_fields"] = missing_required_fields or []
    state["tool_calls"] = [
        ToolCallRecord(
            call_id="market_1",
            request=ToolCallRequest(
                tool_name="mcp.production.stock.market_snapshot",
                arguments={"symbol": "300085.SZ"},
                purpose="获取行情快照",
            ),
            result=ToolCallResult(
                tool_call_id="provider_market_1",
                tool_name="mcp.production.stock.market_snapshot",
                status="success",
                result=envelope,
                trace_id=state["trace_id"],
            ),
        )
    ]
    return analysis_agent_node(state)


@pytest.mark.parametrize(
    ("code", "expected_text"),
    [
        ("market_data_source_not_configured", "未配置市场数据源"),
        ("required_api_permission_missing", "当前数据源权限不足"),
        ("provider_rate_limited", "市场数据服务当前访问频繁"),
    ],
)
def test_synthesizer_renders_safe_market_error(code, expected_text):
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下银之杰",
    )
    state["errors"].append(
        {
            "node": "tool_agent",
            "message": "upstream token=super-secret-value",
            "code": code,
            "http_status": 500,
            "handled": True,
        }
    )

    draft = synthesizer_node(state)["draft_answer"]

    assert expected_text in draft["content"]
    assert "super-secret-value" not in draft["content"]
    assert draft["business_error_code"] == code


def test_synthesizer_builds_equity_claims_from_tool_facts_without_raw_dict():
    state = _equity_market_state(
        data={"symbol": "300085.SZ", "price": 18.23, "pe_ttm": 42.1},
        requested_fields=["price", "pe_ttm"],
        provided_fields=["price", "pe_ttm"],
    )

    draft = synthesizer_node(state)["draft_answer"]

    assert [claim["tool_evidence_refs"][0]["field_path"] for claim in draft["claims"]] == [
        "data.price",
        "data.pe_ttm",
    ]
    assert "18.23" in draft["content"]
    assert "42.1" in draft["content"]
    assert "{'" not in draft["content"]


def test_synthesizer_discloses_optional_market_tool_failure_without_discarding_facts():
    state = _equity_market_state(
        data={"name": "银之杰", "price": 18.23},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"
    state["tool_calls"].append(
        ToolCallRecord(
            call_id="announcement_1",
            request=ToolCallRequest(
                tool_name="mcp.production.stock.announcement_search",
                arguments={
                    "symbol": "300085.SZ",
                    "required_fields": [],
                    "optional_fields": ["announcements"],
                },
                purpose="获取近期公告",
            ),
            result=ToolCallResult(
                tool_call_id="provider_announcement_1",
                tool_name="mcp.production.stock.announcement_search",
                status="failed",
                error=ToolError(
                    code="provider_permission_denied",
                    message="公告接口无权限",
                ),
                trace_id=state["trace_id"],
            ),
        )
    )
    state = analysis_agent_node(state)

    draft = synthesizer_node(state)["draft_answer"]

    assert "银之杰" in draft["content"]
    assert "18.23" in draft["content"]
    assert PARTIAL_MARKET_DATA_DISCLOSURE in draft["content"]
    assert draft["response_policy"]["response_type"] == "equity_research_report"
    assert "business_error_code" not in draft


def test_synthesizer_repairs_equity_llm_rendering_that_drops_tool_claims():
    llm_client = SequencedLlmClient(
        [
            "我已处理完成，但当前没有可展示的文本结果。",
            "核心判断\n银之杰当前可结合已取得的公司与行情事实继续分析。",
        ]
    )
    state = _equity_market_state(
        data={"symbol": "300085.SZ", "name": "银之杰", "price": 18.23},
        requested_fields=["symbol", "name", "price"],
        provided_fields=["symbol", "name", "price"],
    )

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert "银之杰" in draft["content"]
    assert "claim_signals_missing" in llm_client.calls[1]["system_prompt"]
    assert draft["response_policy"]["llm_rendering"] == "repaired"


def test_synthesizer_localizes_and_deduplicates_equity_tool_claims():
    state = _equity_market_state(
        data={
            "name": "银之杰",
            "exchange": "SZ",
            "board": "创业板",
            "registered_address": "广东深圳市",
            "pct_change": 5.24,
            "turnover": 1234.0,
            "market_cap": 5678.0,
        },
        requested_fields=[
            "name",
            "exchange",
            "board",
            "registered_address",
            "pct_change",
            "turnover",
            "market_cap",
        ],
        provided_fields=[
            "name",
            "exchange",
            "board",
            "registered_address",
            "pct_change",
            "turnover",
            "market_cap",
        ],
    )
    state["analysis"]["tool_facts"].append(dict(state["analysis"]["tool_facts"][0]))

    draft = synthesizer_node(state)["draft_answer"]

    assert "exchange为" not in draft["content"]
    assert "board为" not in draft["content"]
    assert "pct_change为" not in draft["content"]
    assert "交易所为SZ" in draft["content"]
    assert "上市板块为创业板" in draft["content"]
    assert "涨跌幅为5.24" in draft["content"]
    assert draft["content"].count("公司名称为银之杰") == 1


def test_verifier_rejects_mismatched_tool_value():
    state = _equity_market_state(
        data={"price": 18.23},
        requested_fields=["price"],
        provided_fields=["price"],
    )
    state["draft_answer"] = {
        "content": f"最新价格为 99.99 元。\n\n{FINANCIAL_DISCLAIMER}",
        "claims": [
            {
                "claim_id": "claim_1",
                "text": "最新价格为 99.99 元",
                "evidence_ids": [],
                "confidence": 1.0,
                "claim_type": "fact",
                "tool_evidence_refs": [
                    {
                        "tool_call_id": "market_1",
                        "field_path": "data.price",
                        "value": 99.99,
                    }
                ],
            }
        ],
        "cited_evidence_ids": [],
        "response_policy": {"citation_required": False},
    }

    result = verifier_node(state)

    assert "tool_evidence_value_mismatch" in result["verification"]["risk_flags"]


def test_verifier_rejects_missing_required_market_field():
    state = _equity_market_state(
        data={},
        requested_fields=["price"],
        provided_fields=[],
        status="partial",
        missing_required_fields=["price"],
    )
    state["draft_answer"] = {
        "content": f"当前没有可确认的价格。\n\n{FINANCIAL_DISCLAIMER}",
        "claims": [],
        "cited_evidence_ids": [],
        "response_policy": {"citation_required": False},
    }

    result = verifier_node(state)

    assert "missing_required_tool_fields" in result["verification"]["risk_flags"]


def test_verifier_rejects_uncited_financial_number():
    state = _equity_market_state(
        data={"price": 18.23},
        requested_fields=["price"],
        provided_fields=["price"],
    )
    state["draft_answer"] = {
        "content": f"最新价格为 99.99 元。\n\n{FINANCIAL_DISCLAIMER}",
        "claims": [
            {
                "claim_id": "claim_1",
                "text": "最新价格为 99.99 元",
                "evidence_ids": [],
                "confidence": 1.0,
                "claim_type": "fact",
            }
        ],
        "cited_evidence_ids": [],
        "response_policy": {"citation_required": False},
    }

    result = verifier_node(state)

    assert "unsupported_financial_number" in result["verification"]["risk_flags"]


def test_verifier_accepts_stock_code_numbers_from_tool_evidence():
    state = _equity_market_state(
        data={"symbol": "300085.SZ", "price": 18.23},
        requested_fields=["symbol", "price"],
        provided_fields=["symbol", "price"],
    )
    state = synthesizer_node(state)

    result = verifier_node(state)

    assert result["verification"]["verdict"] == "pass"
    assert "unsupported_financial_number" not in result["verification"]["risk_flags"]


def test_verifier_rejects_financial_number_added_only_to_rendered_content():
    state = _equity_market_state(
        data={"price": 18.23},
        requested_fields=["price"],
        provided_fields=["price"],
    )
    state = synthesizer_node(state)
    state["draft_answer"]["content"] = (
        f"最新价格为 18.23 元，目标数字为 77.7 元。\n\n{FINANCIAL_DISCLAIMER}"
    )

    result = verifier_node(state)

    assert "unsupported_financial_number" in result["verification"]["risk_flags"]


def test_verifier_requires_partial_market_data_disclosure():
    state = _equity_market_state(
        data={"price": 18.23},
        requested_fields=["price", "pe_ttm"],
        provided_fields=["price"],
        status="partial",
        missing_optional_fields=["pe_ttm"],
    )
    state = synthesizer_node(state)
    state["draft_answer"]["content"] = state["draft_answer"]["content"].replace(
        "部分字段未返回", "数据正常"
    )

    result = verifier_node(state)

    assert "missing_partial_market_data_disclosure" in result["verification"]["risk_flags"]


def test_synthesizer_repairs_llm_equity_number_not_present_in_claims():
    llm_client = SequencedLlmClient(
        [
            "银之杰最新价格为 99.99 元。",
            "行情与估值\n最新价格为 18.23 元。",
        ]
    )
    state = _equity_market_state(
        data={"price": 18.23},
        requested_fields=["price"],
        provided_fields=["price"],
    )

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert "18.23" in draft["content"]
    assert "99.99" not in draft["content"]
    assert "unsupported_numbers" in llm_client.calls[1]["system_prompt"]
    assert draft["response_policy"]["llm_rendering"] == "repaired"


def test_synthesizer_allows_llm_section_numbering_for_equity_answer():
    llm_client = RecordingLlmClient(content="1. 市场概览\n最新价格为 18.23 元。")
    state = _equity_market_state(
        data={"price": 18.23},
        requested_fields=["price"],
        provided_fields=["price"],
    )

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert "1. 市场概览" in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "applied"


def test_synthesizer_accepts_common_equity_section_heading_aliases():
    report = _complete_equity_research_report().replace(
        "核心判断", "核心结论"
    ).replace("财务质量", "财务分析").replace(
        "行情与估值", "市场表现与估值"
    ).replace(
        "主要风险", "风险提示"
    ).replace(
        "数据边界", "分析边界"
    )
    llm_client = SequencedLlmClient([report])
    state = _equity_market_state(
        data={"name": "银之杰"},
        requested_fields=["name"],
        provided_fields=["name"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 1
    assert draft["response_policy"]["llm_rendering"] == "applied"
    assert "核心结论" in draft["content"]


def test_synthesizer_sanitizes_unclaimed_dates_and_accepts_approximate_rounding():
    llm_client = SequencedLlmClient(
        [
            (
                "### 1. 核心判断\n（2）市盈率较高，估值压力需要关注。[tool_claim_2]\n\n"
                "### 2. 业务与行业\n公司名称为岩山科技。[tool_claim_1]\n\n"
                "### 3. 财务质量\n财务数据未获得，无法判断。\n\n"
                "### 4. 行情与估值\n2024年市盈率约470倍，处于较高水平。"
                "[tool_claim_2]\n\n"
                "### 5. 主要风险\n(3)高市盈率意味着估值波动风险较高。[tool_claim_2]\n\n"
                "### 6. 数据边界\n数据截至2026-07-28，公告证据未获得，需补充。"
            )
        ]
    )
    state = _equity_market_state(
        data={"name": "岩山科技", "pe_ttm": 473.3577},
        requested_fields=["name", "pe_ttm"],
        provided_fields=["name", "pe_ttm"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 1, llm_client.calls[-1]["system_prompt"]
    assert draft["response_policy"]["llm_rendering"] == "applied"
    assert "约470倍" in draft["content"]
    assert "2024" not in draft["content"]
    assert "2026" not in draft["content"]


def test_synthesizer_accepts_rounded_and_unit_converted_claim_numbers():
    llm_client = SequencedLlmClient(
        [
            (
                "核心判断\n公司目前盈利为正，但经营现金流为负，盈利质量仍需观察。"
                "[tool_claim_6][tool_claim_7]\n\n"
                "业务与行业\n公司名称为岩山科技。[tool_claim_1]\n\n"
                "财务质量\n营业收入约4.83亿元，归母净利润约4234.85万元，"
                "经营活动现金流净流出约6855.68万元。"
                "[tool_claim_5][tool_claim_6][tool_claim_7]"
                "毛利率约33.48%，净资产收益率约0.33%。"
                "[tool_claim_8][tool_claim_9]\n\n"
                "行情与估值\n当日涨跌幅约9.98%，总市值约387.20亿元，"
                "市盈率约473.36倍。[tool_claim_2][tool_claim_3][tool_claim_4]\n\n"
                "主要风险\n经营现金流为负，同时市盈率较高，"
                "需关注业绩与估值匹配风险。[tool_claim_7][tool_claim_4]\n\n"
                "数据边界\n未获得公告证据，需补充核验。"
            )
        ]
    )
    state = _equity_market_state(
        data={
            "name": "岩山科技",
            "pct_change": 9.9839,
            "market_cap": 38_720_487_324.0,
            "pe_ttm": 473.3577,
            "revenue": 482_962_078.43,
            "net_profit_parent": 42_348_450.04,
            "operating_cash_flow": -68_556_807.15,
            "gross_margin": 0.334769,
            "roe": 0.003325,
        },
        requested_fields=[
            "name",
            "pct_change",
            "market_cap",
            "pe_ttm",
            "revenue",
            "net_profit_parent",
            "operating_cash_flow",
            "gross_margin",
            "roe",
        ],
        provided_fields=[
            "name",
            "pct_change",
            "market_cap",
            "pe_ttm",
            "revenue",
            "net_profit_parent",
            "operating_cash_flow",
            "gross_margin",
            "roe",
        ],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 1
    assert draft["response_policy"]["llm_rendering"] == "applied"
    assert "营业收入约4.83亿元" in draft["content"]


def test_synthesizer_uses_llm_to_summarize_dynamic_equity_research_facts():
    llm_client = RecordingLlmClient(
        content=(
            "核心判断\n银之杰可以从业务、行情和估值三条线看。"
            "[tool_claim_1][tool_claim_2][tool_claim_3][tool_claim_4]\n\n"
            "业务与行业\n公司名称为银之杰，主营业务为金融信息化服务。"
            "[tool_claim_1][tool_claim_2]\n\n"
            "财务质量\n财务数据未获得，无法判断。\n\n"
            "行情与估值\n最新价格为18.23，市盈率为42.1。"
            "[tool_claim_3][tool_claim_4]\n\n"
            "主要风险\n公司公告显示公司关注金融科技业务进展。"
            "[evidence_claim_5]\n\n"
            "数据边界\n财务数据未获得，需补充。"
        )
    )
    state = _equity_market_state(
        data={
            "name": "银之杰",
            "main_business": "金融信息化服务",
            "price": 18.23,
            "pe_ttm": 42.1,
        },
        requested_fields=["name", "main_business", "price", "pe_ttm"],
        provided_fields=["name", "main_business", "price", "pe_ttm"],
    )
    state["user_message"] = "分析一下银之杰"
    state["plan"]["analysis_dimensions"] = ["business", "market", "valuation"]
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["分析一下银之杰"],
        coverage="sufficient",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                title="公告摘要",
                content="公司公告显示公司关注金融科技业务进展。",
                score=0.82,
            )
        ],
    )
    state = analysis_agent_node(state)

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert "业务、行情和估值" in draft["content"]
    assert "金融信息化服务" in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "applied"
    assert "analysis_dimensions" in llm_client.calls[0]["user_prompt"]


def test_synthesizer_requests_evidence_grounded_equity_analysis():
    llm_client = SequencedLlmClient(
        [
            (
                "核心判断\n公司当前盈利与现金流均承压。"
                "[tool_claim_2][tool_claim_3]\n\n"
                "业务与行业\n公司名称为银之杰。[tool_claim_1]\n\n"
                "财务质量\n归母净利润为-62997994.15，"
                "经营活动现金流为-46030115.76，盈利与现金流均承压。"
                "[tool_claim_2][tool_claim_3]\n\n"
                "行情与估值\n行情和估值数据未获得，无法判断。\n\n"
                "主要风险\n盈利与现金流持续承压构成经营风险。"
                "[tool_claim_2][tool_claim_3]\n\n"
                "数据边界\n未获得的指标和信息不作推断，需补充。"
            )
        ]
    )
    state = _equity_market_state(
        data={
            "name": "银之杰",
            "net_profit_parent": -62997994.15,
            "operating_cash_flow": -46030115.76,
        },
        requested_fields=["name", "net_profit_parent", "operating_cash_flow"],
        provided_fields=["name", "net_profit_parent", "operating_cash_flow"],
    )
    state["user_message"] = "分析一下银之杰"
    state["plan"]["analysis_dimensions"] = [
        "business",
        "financial",
        "market",
        "valuation",
        "risk",
    ]
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert "盈利与现金流均承压" in draft["content"]
    system_prompt = llm_client.calls[0]["system_prompt"]
    assert "证据约束下的 A 股投研分析师" in system_prompt
    assert all(
        section in system_prompt
        for section in (
            "核心判断",
            "业务与行业",
            "财务质量",
            "行情与估值",
            "主要风险",
            "数据边界",
        )
    )
    assert "不要逐字段复述" in system_prompt
    assert "盈利承压" in system_prompt
    assert "经营现金流承压" in system_prompt
    assert "估值指标较高" in system_prompt
    assert "[tool_claim_1]" in system_prompt
    assert "段落末尾" in system_prompt
    assert llm_client.calls[0]["temperature"] == 0.0
    assert llm_client.calls[0]["max_tokens"] == 2048
    user_prompt = llm_client.calls[0]["user_prompt"]
    assert all(
        field in user_prompt
        for field in (
            "claims",
            "user_message",
            "analysis_dimensions",
            "structured_tool_results",
            "data_coverage",
        )
    )
    assert draft["response_policy"]["llm_rendering"] == "applied"
    assert "tool_claim_" not in draft["content"]


def test_synthesizer_accepts_claim_bound_report_and_hides_internal_references():
    llm_client = SequencedLlmClient(
        [
            (
                "核心判断\n盈利与经营活动现金流均承压。"
                "[tool_claim_2][tool_claim_3]\n\n"
                "业务与行业\n公司名称为银之杰。[tool_claim_1]\n\n"
                "财务质量\n归母净利润为-62997994.15，"
                "经营活动现金流为-46030115.76。"
                "[tool_claim_2][tool_claim_3]\n\n"
                "行情与估值\n行情和估值数据未获得，无法判断。\n\n"
                "主要风险\n负利润与负经营活动现金流反映经营压力。"
                "[tool_claim_2][tool_claim_3]\n\n"
                "数据边界\n行业和估值数据未获得，需补充。"
            )
        ]
    )
    state = _equity_market_state(
        data={
            "name": "银之杰",
            "net_profit_parent": -62997994.15,
            "operating_cash_flow": -46030115.76,
        },
        requested_fields=["name", "net_profit_parent", "operating_cash_flow"],
        provided_fields=["name", "net_profit_parent", "operating_cash_flow"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert draft["response_policy"]["llm_rendering"] == "applied"
    assert "盈利与经营活动现金流均承压" in draft["content"]
    assert "tool_claim_" not in draft["content"]


def test_synthesizer_accepts_paragraph_level_claim_references():
    llm_client = SequencedLlmClient(
        [
            (
                "核心判断\n公司盈利为正。经营现金流为负，盈利质量承压。"
                "[tool_claim_2][tool_claim_3]\n\n"
                "业务与行业\n公司名称为岩山科技。[tool_claim_1]\n\n"
                "财务质量\n归母净利润为42348450.04。"
                "经营活动现金流为-68556807.15，现金回款质量需要关注。"
                "[tool_claim_2][tool_claim_3]\n\n"
                "行情与估值\n行情和估值数据未获得，无法判断。\n\n"
                "主要风险\n经营活动现金流为负，构成经营风险。[tool_claim_3]\n\n"
                "数据边界\n公告证据未获得，需补充。"
            )
        ]
    )
    state = _equity_market_state(
        data={
            "name": "岩山科技",
            "net_profit_parent": 42_348_450.04,
            "operating_cash_flow": -68_556_807.15,
        },
        requested_fields=["name", "net_profit_parent", "operating_cash_flow"],
        provided_fields=["name", "net_profit_parent", "operating_cash_flow"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 1
    assert draft["response_policy"]["llm_rendering"] == "applied"
    assert "现金回款质量需要关注" in draft["content"]


def test_synthesizer_rejects_price_claim_presented_as_pe_ratio():
    invalid_report = (
        "核心判断\n当前仅能确认公司名称为银之杰。[tool_claim_1]\n\n"
        "业务与行业\n公司名称为银之杰。[tool_claim_1]\n\n"
        "财务质量\n财务数据未获得，无法判断。\n\n"
        "行情与估值\n市盈率为18.23倍。[tool_claim_2]\n\n"
        "主要风险\n风险证据未获得，无法判断。\n\n"
        "数据边界\n当前数据不足，需补充。"
    )
    llm_client = SequencedLlmClient(
        [invalid_report, _minimal_claim_bound_equity_report()]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 18.23},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 2
    assert "unsupported_section_analysis" in llm_client.calls[1]["system_prompt"]
    assert "市盈率为18.23" not in draft["content"]


def test_synthesizer_rejects_number_from_unreferenced_claim_in_section():
    invalid_report = (
        "核心判断\n当前仅能确认公司名称为银之杰。[tool_claim_1]\n\n"
        "业务与行业\n公司名称为银之杰。[tool_claim_1]\n\n"
        "财务质量\n财务数据未获得，无法判断。\n\n"
        "行情与估值\n最新价格为42.1。[tool_claim_2]\n\n"
        "主要风险\n风险证据未获得，无法判断。\n\n"
        "数据边界\n当前数据不足，需补充。"
    )
    llm_client = SequencedLlmClient(
        [invalid_report, _minimal_claim_bound_equity_report()]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 18.23, "pe_ttm": 42.1},
        requested_fields=["name", "price", "pe_ttm"],
        provided_fields=["name", "price", "pe_ttm"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    build_synthesizer_node(llm_client=llm_client)(state)

    assert len(llm_client.calls) == 2
    assert "mismatched_claim_number" in llm_client.calls[1]["system_prompt"]


def test_synthesizer_rejects_financial_analysis_without_financial_claim():
    invalid_report = (
        "核心判断\n当前仅能确认公司名称和价格。"
        "[tool_claim_1][tool_claim_2]\n\n"
        "业务与行业\n公司名称为银之杰。[tool_claim_1]\n\n"
        "财务质量\n盈利与现金流承压。[tool_claim_1][tool_claim_2]\n\n"
        "行情与估值\n最新价格为27.44。[tool_claim_2]\n\n"
        "主要风险\n风险证据未获得，无法判断。\n\n"
        "数据边界\n财务数据未获得，需补充。"
    )
    llm_client = SequencedLlmClient(
        [invalid_report, _minimal_claim_bound_equity_report()]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    build_synthesizer_node(llm_client=llm_client)(state)

    assert len(llm_client.calls) == 2
    assert "unsupported_section_analysis" in llm_client.calls[1]["system_prompt"]


def test_synthesizer_rejects_empty_equity_report_sections():
    llm_client = SequencedLlmClient(
        [
            (
                "核心判断\n银之杰。[tool_claim_1]\n\n业务与行业\n\n财务质量\n\n"
                "行情与估值\n\n主要风险\n\n数据边界\n"
            ),
            _minimal_claim_bound_equity_report(),
        ]
    )
    state = _equity_market_state(
        data={"name": "银之杰"},
        requested_fields=["name"],
        provided_fields=["name"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    build_synthesizer_node(llm_client=llm_client)(state)

    assert len(llm_client.calls) == 2
    assert "missing_required_sections" in llm_client.calls[1]["system_prompt"]


def test_synthesizer_rejects_unknown_claim_reference():
    invalid_report = _minimal_claim_bound_equity_report().replace(
        "[tool_claim_1]", "[tool_claim_999]"
    )
    llm_client = SequencedLlmClient(
        [invalid_report, _minimal_claim_bound_equity_report()]
    )
    state = _equity_market_state(
        data={"name": "银之杰"},
        requested_fields=["name"],
        provided_fields=["name"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    build_synthesizer_node(llm_client=llm_client)(state)

    assert len(llm_client.calls) == 2
    assert "invalid_claim_reference" in llm_client.calls[1]["system_prompt"]


@pytest.mark.parametrize(
    "invalid_content",
    [
        "目标价为27.44元。",
        "预计股价将确定性上涨。",
        "承诺投资者可以获得稳定收益。",
        "建议直接买入银之杰。",
    ],
)
def test_synthesizer_repairs_prohibited_financial_advice(invalid_content):
    llm_client = SequencedLlmClient(
        [invalid_content, _complete_equity_research_report()]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 2
    assert "prohibited_financial_advice" in llm_client.calls[1]["system_prompt"]
    assert invalid_content not in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "repaired"


@pytest.mark.parametrize(
    "directional_prediction",
    [
        "股价未来有望上涨。",
        "股价未来有望下跌。",
        "后续或将上涨。",
        "后续或将下跌。",
        "短期看涨。",
        "短期看跌。",
    ],
)
def test_synthesizer_repairs_directional_price_prediction(directional_prediction):
    invalid_report = _complete_equity_research_report().replace(
        "行情与估值数据未获得，无法判断。",
        f"{directional_prediction}[tool_claim_2]",
    )
    llm_client = SequencedLlmClient(
        [invalid_report, _complete_equity_research_report()]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 2
    assert "prohibited_financial_advice" in llm_client.calls[1]["system_prompt"]
    assert directional_prediction not in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "repaired"


@pytest.mark.parametrize(
    "implicit_advice",
    [
        "合理估值中枢为27.44。[tool_claim_2]",
        "当前仍有上行空间。[tool_claim_2]",
        "后续存在下行空间。[tool_claim_2]",
        "适合逢低布局。[tool_claim_2]",
        "可以低吸。[tool_claim_2]",
        "建议高抛。[tool_claim_2]",
        "当前存在配置机会。[tool_claim_2]",
    ],
)
def test_synthesizer_repairs_implicit_financial_advice_bypass(implicit_advice):
    invalid_report = _minimal_claim_bound_equity_report().replace(
        "行情与估值数据未获得，无法判断。",
        implicit_advice,
    )
    llm_client = SequencedLlmClient(
        [invalid_report, _minimal_claim_bound_equity_report()]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    build_synthesizer_node(llm_client=llm_client)(state)

    assert len(llm_client.calls) == 2
    assert "prohibited_financial_advice" in llm_client.calls[1]["system_prompt"]


def test_synthesizer_allows_neutral_price_volatility_risk_description():
    neutral_report = _complete_equity_research_report().replace(
        "风险证据未获得，无法判断。",
        "市场风险可能导致股价波动。[tool_claim_2]",
    )
    llm_client = SequencedLlmClient([neutral_report])
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 1
    assert "市场风险可能导致股价波动" in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "applied"


def test_synthesizer_appends_financial_disclaimer_after_llm_validation():
    llm_client = SequencedLlmClient([_minimal_claim_bound_equity_report()])
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 1
    assert draft["response_policy"]["llm_rendering"] == "applied"
    assert FINANCIAL_DISCLAIMER in draft["content"]


@pytest.mark.parametrize(
    "brand_self_report",
    [
        "我是 ChatGPT，以下是分析。",
        "我是 Claude，以下是分析。",
        "我是 Gemini，以下是分析。",
        "我是通义千问，以下是分析。",
    ],
)
def test_synthesizer_repairs_common_model_brand_self_report(brand_self_report):
    llm_client = SequencedLlmClient(
        [
            f"{brand_self_report}\n{_complete_equity_research_report()}",
            _complete_equity_research_report(),
        ]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 2
    assert "prohibited_model_brand" in llm_client.calls[1]["system_prompt"]
    assert brand_self_report not in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "repaired"


@pytest.mark.parametrize(
    "brand_statement",
    [
        "OpenAI 分析结果如下。",
        "Claude 分析如下。",
        "智谱分析如下。",
        "讯飞星火分析如下。",
        "MiniMax 分析如下。",
        "Llama 分析如下。",
        "Mistral 分析如下。",
    ],
)
def test_synthesizer_repairs_model_brand_without_self_report_prefix(brand_statement):
    llm_client = SequencedLlmClient(
        [
            f"{brand_statement}\n{_complete_equity_research_report()}",
            _complete_equity_research_report(),
        ]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 2
    assert "prohibited_model_brand" in llm_client.calls[1]["system_prompt"]
    assert brand_statement not in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "repaired"


def test_synthesizer_repairs_unsupported_company_event():
    invalid_report = _complete_equity_research_report().replace(
        "风险证据未获得，无法判断。",
        "公司涉及诉讼。",
    )
    llm_client = SequencedLlmClient(
        [invalid_report, _complete_equity_research_report()]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 2
    assert "unsupported_company_event" in llm_client.calls[1]["system_prompt"]
    assert "涉及诉讼" not in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "repaired"


@pytest.mark.parametrize(
    "unsupported_event",
    [
        "公司正在推进收购。",
        "公司筹划重大资产购买。",
        "公司发生控制权变更。",
        "公司股票即将停牌。",
        "公司收到监管问询。",
        "公司涉嫌财务造假。",
    ],
)
def test_synthesizer_repairs_additional_unsupported_company_event(
    unsupported_event,
):
    invalid_report = _complete_equity_research_report().replace(
        "风险证据未获得，无法判断。",
        unsupported_event,
    )
    llm_client = SequencedLlmClient(
        [invalid_report, _complete_equity_research_report()]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 2
    assert "unsupported_company_event" in llm_client.calls[1]["system_prompt"]
    assert unsupported_event not in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "repaired"


def test_synthesizer_rejects_positive_event_from_negated_claim():
    invalid_report = _minimal_claim_bound_equity_report().replace(
        "风险证据未获得，无法判断。",
        "公司发生重大诉讼。[tool_claim_2]",
    )
    llm_client = SequencedLlmClient(
        [invalid_report, _minimal_claim_bound_equity_report()]
    )
    state = _equity_market_state(
        data={
            "name": "银之杰",
            "announcements": [{"title": "无重大诉讼", "date": "日期未知"}],
        },
        requested_fields=["name", "announcements"],
        provided_fields=["name", "announcements"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 2
    assert "unsupported_company_event" in llm_client.calls[1]["system_prompt"]
    assert "发生重大诉讼" not in draft["content"]


def test_synthesizer_allows_missing_event_evidence_boundary_statement():
    boundary_report = _minimal_claim_bound_equity_report().replace(
        "风险证据未获得，无法判断。",
        "未获得诉讼证据，无法判断是否存在诉讼。",
    )
    llm_client = SequencedLlmClient([boundary_report])
    state = _equity_market_state(
        data={"name": "银之杰"},
        requested_fields=["name"],
        provided_fields=["name"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 1
    assert "未获得诉讼证据" in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "applied"


def test_synthesizer_repairs_equity_report_missing_required_sections():
    llm_client = SequencedLlmClient(
        [
            "核心判断\n银之杰盈利承压。\n\n财务质量\n盈利能力仍有压力。",
            _complete_equity_research_report(),
        ]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 2
    assert "missing_required_sections" in llm_client.calls[1]["system_prompt"]
    assert all(
        section in draft["content"]
        for section in (
            "核心判断",
            "业务与行业",
            "财务质量",
            "行情与估值",
            "主要风险",
            "数据边界",
        )
    )
    assert draft["response_policy"]["llm_rendering"] == "repaired"


def test_synthesizer_does_not_require_six_sections_for_risk_scan():
    llm_client = SequencedLlmClient(["风险扫描\n银之杰当前需关注经营风险。"])
    state = _equity_market_state(
        data={"name": "银之杰"},
        requested_fields=["name"],
        provided_fields=["name"],
    )
    state["plan"]["response_policy"]["response_type"] = (
        "equity_research_risk_scan"
    )

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 1
    assert draft["response_policy"]["llm_rendering"] == "applied"


def test_synthesizer_repairs_invalid_equity_analysis_once():
    llm_client = SequencedLlmClient(
        [
            "银之杰目标价格为99.99元。",
            (
                "核心判断\n公司盈利承压。[tool_claim_2]\n\n"
                "业务与行业\n公司名称为银之杰。[tool_claim_1]\n\n"
                "财务质量\n归母净利润为-62997994.15，盈利承压。"
                "[tool_claim_2]\n\n"
                "行情与估值\n行情与估值数据未获得，无法判断。\n\n"
                "主要风险\n负利润反映经营压力。[tool_claim_2]\n\n"
                "数据边界\n行业和行情数据未获得，需补充。"
            ),
        ]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "net_profit_parent": -62997994.15},
        requested_fields=["name", "net_profit_parent"],
        provided_fields=["name", "net_profit_parent"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 2
    assert llm_client.calls[1]["temperature"] == 0.0
    assert llm_client.calls[1]["max_tokens"] == 2048
    assert "unsupported_numbers" in llm_client.calls[1]["system_prompt"]
    assert "99.99" not in draft["content"]
    assert "盈利承压" in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "repaired"
    assert draft["response_policy"]["llm_provider"] == "fake"
    assert draft["response_policy"]["llm_model"] == "fake-research-model"
    assert draft["response_policy"]["llm_usage"] == {"total_tokens": 16}


def test_synthesizer_uses_deterministic_facts_after_failed_llm_repair():
    llm_client = SequencedLlmClient(
        [
            "目标价格为99.99元。",
            "预计上涨88.88%。",
        ]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 2
    assert "99.99" not in draft["content"]
    assert "88.88" not in draft["content"]
    assert "公司名称为银之杰" in draft["content"]
    assert "最新价格为27.44" in draft["content"]
    assert "核心判断" in draft["content"]
    assert "数据边界" in draft["content"]
    assert "无法在现有证据约束内生成可靠的综合分析" not in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "deterministic_fallback"
    assert (
        draft["response_policy"]["llm_validation_error"]
        == "unsupported_numbers"
    )


def test_synthesizer_salvages_supported_sections_after_failed_repair():
    report = (
        "核心判断\n公司归母净利润为正，但现金流承压。"
        "[tool_claim_2][tool_claim_3]\n\n"
        "业务与行业\n公司名称为岩山科技。[tool_claim_1]\n\n"
        "财务质量\n净资产收益率为99.99%。[tool_claim_2]"
        "经营活动现金流为-68556807.15，现金流承压。"
        "[tool_claim_3]\n\n"
        "行情与估值\n行情和估值数据未获得，无法判断。\n\n"
        "主要风险\n经营活动现金流为负，需关注经营风险。[tool_claim_3]\n\n"
        "数据边界\n公告证据未获得，需补充。"
    )
    llm_client = SequencedLlmClient([report])
    state = _equity_market_state(
        data={
            "name": "岩山科技",
            "net_profit_parent": 42_348_450.04,
            "operating_cash_flow": -68_556_807.15,
        },
        requested_fields=["name", "net_profit_parent", "operating_cash_flow"],
        provided_fields=["name", "net_profit_parent", "operating_cash_flow"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert len(llm_client.calls) == 1
    assert draft["response_policy"]["llm_rendering"] == "salvaged"
    assert "99.99" not in draft["content"]
    assert "现金流承压" in draft["content"]
    assert "无法在现有证据约束内" not in draft["content"]


def test_synthesizer_uses_safe_fallback_when_primary_llm_call_fails():
    llm_client = SequencedFailingLlmClient(
        [RuntimeError("provider-primary-secret")]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert "无法在现有证据约束内生成可靠的综合分析" in draft["content"]
    assert "provider-primary-secret" not in draft["content"]
    assert "公司概览" not in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "safe_fallback"
    assert draft["response_policy"]["llm_validation_error"] == "primary_call_failed"


def test_synthesizer_preserves_first_validation_error_when_repair_call_fails():
    llm_client = SequencedFailingLlmClient(
        ["目标价格为99.99元。", RuntimeError("provider-repair-secret")]
    )
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=llm_client)(state)["draft_answer"]

    assert "公司名称为银之杰" in draft["content"]
    assert "最新价格为27.44" in draft["content"]
    assert "无法在现有证据约束内生成可靠的综合分析" not in draft["content"]
    assert "provider-repair-secret" not in draft["content"]
    assert "核心判断" in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "deterministic_fallback"
    assert draft["response_policy"]["llm_validation_error"] == "unsupported_numbers"


def test_image_analysis_nodes_use_attachment_context_without_rag_evidence():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析这张财报截图",
    )
    state["image_analyses"] = [
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
            "financial_metrics": [
                {"name": "营业收入", "value": "100", "unit": "亿元"}
            ],
            "chart_observations": ["同比增长"],
            "entities": ["营业收入"],
            "risk_flags": ["缺少审计意见"],
            "confidence": 0.9,
            "truncated": False,
        }
    ]
    state["intent"] = {
        "intent": "knowledge_qa",
        "domain": "a_share_equity_research",
        "risk_level": "medium",
        "confidence": 0.8,
    }
    state = build_planner_agent_node(available_tools=[])(state)

    state = analysis_agent_node(state)
    state = synthesizer_node(state)
    state = verifier_node(state)

    assert state["analysis"]["findings"] == ["营业收入同比增长。"]
    assert "营业收入同比增长" in state["draft_answer"]["content"]
    assert "不构成投资建议" in state["draft_answer"]["content"]
    assert state["verification"]["verdict"] == "pass"


def test_analysis_agent_merges_image_rag_and_stock_tool_results():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="结合图片和公告分析",
    )
    state["image_analyses"] = [
        {
            "asset_id": "asset-1",
            "analysis_id": "analysis-1",
            "media_type": "image/png",
            "width": 100,
            "height": 80,
            "status": "ready",
            "document_type": "financial_statement",
            "summary": "图片显示营业收入增长。",
            "extracted_text": "",
            "financial_metrics": [],
            "chart_observations": [],
            "entities": ["贵州茅台"],
            "risk_flags": [],
            "confidence": 0.9,
            "truncated": False,
        }
    ]
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp-1",
        query_set=["公告"],
        coverage="sufficient",
        items=[
            EvidenceItem(
                evidence_id="ev-1",
                chunk_id="chunk-1",
                document_id="doc-1",
                source_uri="kb://announcement",
                title="公告",
                content="公告披露产能扩张计划。",
                score=0.8,
            )
        ],
    )
    state["tool_calls"] = [
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.mock.stock.company_profile",
                arguments={"symbol": "600519"},
                purpose="查询公司资料",
            ),
            result=ToolCallResult(
                tool_call_id="call-1",
                tool_name="mcp.mock.stock.company_profile",
                status="success",
                result=_market_data_envelope(
                    data={"symbol": "600519", "name": "贵州茅台"},
                    requested_fields=["name"],
                    provided_fields=["name"],
                    source_apis=["stock_basic"],
                ),
                trace_id=state["trace_id"],
            ),
        )
    ]

    result = analysis_agent_node(state)

    assert result["analysis"]["findings"] == [
        "图片显示营业收入增长。",
        "公告披露产能扩张计划。",
    ]
    assert result["analysis"]["structured_tool_results"][0]["result"]["symbol"] == "600519"


def test_synthesizer_discloses_partial_image_analysis_limit():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析图片",
    )
    state["image_analyses"] = [
        {
            "asset_id": "asset-1",
            "analysis_id": "analysis-1",
            "media_type": "image/png",
            "width": 100,
            "height": 80,
            "status": "partial",
            "document_type": "financial_statement",
            "summary": "OCR 识别到营业收入。",
            "extracted_text": "营业收入",
            "financial_metrics": [],
            "chart_observations": [],
            "entities": [],
            "risk_flags": ["vision_timeout"],
            "confidence": 0.6,
            "truncated": False,
        }
    ]
    state["plan"] = {
        "execution_mode": "image_analysis",
        "response_policy": {
            "citation_required": False,
            "disclaimer_required": True,
        },
    }
    state = analysis_agent_node(state)

    result = synthesizer_node(state)

    assert "图片分析结果仅部分完成" in result["draft_answer"]["content"]
    assert "不构成投资建议" in result["draft_answer"]["content"]


def test_verifier_rejects_partial_image_answer_without_limit_disclosure():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="结合图片分析公司",
    )
    state["intent"] = {
        "intent": "task_workflow",
        "domain": "a_share_equity_research",
    }
    state["image_analyses"] = [
        {
            "asset_id": "asset-1",
            "analysis_id": "analysis-1",
            "media_type": "image/png",
            "width": 100,
            "height": 80,
            "status": "partial",
            "document_type": "financial_statement",
            "summary": "营业收入增长。",
            "extracted_text": "",
            "financial_metrics": [],
            "chart_observations": [],
            "entities": [],
            "risk_flags": ["vision_timeout"],
            "confidence": 0.6,
            "truncated": False,
        }
    ]
    state["plan"] = {
        "execution_mode": "skill_execution",
        "response_policy": {
            "citation_required": False,
            "disclaimer_required": True,
        },
    }
    state["draft_answer"] = {
        "content": "营业收入增长。不构成投资建议。",
        "cited_evidence_ids": [],
    }

    result = verifier_node(state)

    assert result["verification"]["verdict"] == "needs_revision"
    assert "missing_partial_image_disclosure" in result["verification"]["risk_flags"]


def test_synthesizer_and_verifier_preserve_ocr_vision_conflict_warning():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析图片",
    )
    state["image_analyses"] = [
        {
            "asset_id": "asset-1",
            "analysis_id": "analysis-1",
            "media_type": "image/png",
            "width": 100,
            "height": 80,
            "status": "ready",
            "document_type": "financial_statement",
            "summary": "营业收入指标待核验。",
            "extracted_text": "",
            "financial_metrics": [],
            "chart_observations": [],
            "entities": [],
            "risk_flags": ["ocr_vision_metric_conflict:营业收入"],
            "confidence": 0.6,
            "truncated": False,
        }
    ]
    state["plan"] = {
        "execution_mode": "image_analysis",
        "response_policy": {
            "citation_required": False,
            "disclaimer_required": True,
        },
    }
    state = analysis_agent_node(state)

    state = synthesizer_node(state)
    state = verifier_node(state)

    assert "OCR 与视觉模型" in state["draft_answer"]["content"]
    assert "识别存在冲突" in state["draft_answer"]["content"]
    assert state["verification"]["verdict"] == "pass"


def test_synthesizer_adds_financial_disclaimer_for_equity_research_answer():
    state = _equity_market_state(
        data={
            "symbol": "600519.SH",
            "name": "贵州茅台",
            "industry": "食品饮料",
            "main_business": "茅台酒及系列酒的生产与销售。",
        },
        requested_fields=["name", "industry", "main_business"],
        provided_fields=["name", "industry", "main_business"],
    )

    result = synthesizer_node(state)

    assert "公司概览" in result["draft_answer"]["content"]
    assert "贵州茅台" in result["draft_answer"]["content"]
    assert "不构成投资建议" in result["draft_answer"]["content"]


def _configured_skill(
    *,
    allowed_tools: list[str],
    risk_level: str = "medium",
    evidence_contract=None,
    evidence_contract_error: str | None = None,
):
    from app.skills.configured import ConfiguredSkill
    from app.skills.definitions import SkillDefinition, SkillExecutionGuide

    if evidence_contract is None:
        requirements = []
        default_fields = {
            "resolve_company": ["symbol"],
            "company_profile": ["name"],
            "market_snapshot": ["price"],
            "financial_metrics": ["revenue"],
            "announcement_search": ["announcements"],
        }
        for tool_name in allowed_tools:
            capability_name = tool_name.rsplit(".", maxsplit=1)[-1]
            if capability_name in default_fields and ".stock." in tool_name:
                requirements.append(
                    {
                        "capability": f"stock.{capability_name}",
                        "required_fields": default_fields[capability_name],
                        "optional_fields": [],
                    }
                )
        evidence_contract = {"requirements": requirements}

    return ConfiguredSkill(
        definition=SkillDefinition(
            name="company_snapshot",
            version="1.0.0",
            description="生成 A 股公司速览",
            input_contract={
                "supported_domains": ["a_share_equity_research"],
                "supported_sub_intents": ["company_snapshot"],
                "supported_intents": ["task_workflow"],
            },
            output_contract={},
            allowed_tools=allowed_tools,
            constraints=["必须追加投研免责声明"],
            failure_modes=[],
            evidence_contract=evidence_contract,
            evidence_contract_error=evidence_contract_error,
            definition_hash="hash_definition",
        ),
        guide=SkillExecutionGuide(
            name="company_snapshot",
            version="1.0.0",
            instructions=[],
            required_evidence_policy="answer_prefers_cited_evidence",
            definition_hash="hash_guide",
        ),
        risk_level=risk_level,
    )


def test_analysis_synthesizer_and_verifier_pass_with_cited_evidence():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
    )
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["差旅报销需要什么？"],
        coverage="sufficient",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                source_uri="kb://finance",
                title="差旅制度",
                content="差旅报销需要提交发票和审批单。",
                score=0.95,
            )
        ],
    )

    state = analysis_agent_node(state)
    state = synthesizer_node(state)
    state = verifier_node(state)

    assert "发票和审批单" in state["draft_answer"]["content"]
    assert state["verification"]["verdict"] == "pass"


def test_initial_state_has_no_memory_context():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="What documents are required for reimbursement?",
    )
    assert "memory_context" not in state


def test_synthesizer_renders_direct_response_plan_without_evidence():
    llm_client = RecordingLlmClient(content="你好，我在。继续告诉我你的目标。")
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="你好",
    )
    state["intent"] = {
        "intent": "conversation",
        "risk_level": "low",
        "needs_clarification": False,
        "confidence": 0.9,
        "reason_codes": ["rule_conversation"],
        "next_node": "planner_agent",
    }
    state["plan"] = {
        "execution_mode": "direct_response",
        "response_policy": {
            "citation_required": False,
            "response_type": "conversation_response",
            "use_llm": True,
        },
    }

    result = build_synthesizer_node(llm_client=llm_client)(state)

    assert result["draft_answer"]["content"] == "你好，我在。继续告诉我你的目标。"
    assert result["draft_answer"]["cited_evidence_ids"] == []
    assert result["draft_answer"]["response_policy"]["citation_required"] is False
    assert result["draft_answer"]["response_policy"]["llm_rendering"] == "applied"


def test_synthesizer_renders_successful_tool_execution_result():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="计算1加到50",
    )
    state["plan"] = {
        "execution_mode": "tool_execution",
        "response_policy": {
            "citation_required": False,
        },
    }
    state["tool_calls"] = [
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.mock.echo",
                tool_version="mcp-http-v1",
                arguments={"numbers": list(range(1, 51))},
                purpose="计算 1 到 50 的总和",
            ),
            result={
                "tool_call_id": "tool_1",
                "tool_name": "mcp.mock.echo",
                "status": "success",
                "result": {"total": 1275, "count": 50},
                "trace_id": "trace_1",
            },
        )
    ]

    result = synthesizer_node(state)

    assert "mcp.mock.echo" not in result["draft_answer"]["content"]
    assert "计算 1 到 50 的总和" in result["draft_answer"]["content"]
    assert "1275" in result["draft_answer"]["content"]
    assert result["draft_answer"]["response_policy"]["citation_required"] is False
    assert result["draft_answer"]["response_policy"]["tool_result_rendering"] == "fallback"


def test_synthesizer_renders_tool_result_with_product_voice():
    llm_client = RecordingLlmClient(content="1+2+3 的结果是 6。")
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="1+2+3=?",
    )
    state["plan"] = {
        "execution_mode": "tool_execution",
        "response_policy": {
            "citation_required": False,
        },
    }
    state["tool_calls"] = [
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.mock.echo",
                tool_version="mcp-http-v1",
                arguments={"numbers": [1, 2, 3]},
                purpose="计算用户给出的数字总和",
            ),
            result={
                "tool_call_id": "tool_1",
                "tool_name": "mcp.mock.echo",
                "status": "success",
                "result": {"total": 6, "count": 3},
                "trace_id": "trace_1",
            },
        )
    ]

    result = build_synthesizer_node(llm_client=llm_client)(state)

    assert result["draft_answer"]["content"] == "1+2+3 的结果是 6。"
    assert "mcp.mock.echo" not in result["draft_answer"]["content"]
    assert result["draft_answer"]["response_policy"]["tool_result_rendering"] == "applied"
    assert "工具结果回答渲染器" in llm_client.calls[0]["system_prompt"]


def test_synthesizer_normalizes_provider_brand_from_llm_response():
    llm_client = RecordingLlmClient(content="我是 DeepSeek，很高兴为你服务。你好，我在。")
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="你好",
    )
    state["intent"] = {
        "intent": "conversation",
        "risk_level": "low",
        "needs_clarification": False,
        "confidence": 0.9,
        "reason_codes": ["rule_conversation"],
        "next_node": "planner_agent",
    }
    state["plan"] = {
        "execution_mode": "direct_response",
        "response_policy": {
            "citation_required": False,
            "response_type": "conversation_response",
            "use_llm": True,
        },
    }

    result = build_synthesizer_node(llm_client=llm_client)(state)

    assert "DeepSeek" not in result["draft_answer"]["content"]
    assert "大语言模型" not in result["draft_answer"]["content"]
    assert "你好" in result["draft_answer"]["content"]


def test_verifier_passes_direct_response_when_citations_are_not_required():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="你好",
    )
    state["plan"] = {
        "response_policy": {
            "citation_required": False,
        },
    }
    state["draft_answer"] = {
        "content": "你好，我在。",
        "claims": [],
        "cited_evidence_ids": [],
        "missing_information": [],
        "assumptions": [],
        "confidence": 0.9,
        "response_policy": {"citation_required": False},
    }

    result = verifier_node(state)

    assert result["verification"]["verdict"] == "pass"
    assert result["verification"]["required_action"] == "respond"


def test_synthesizer_emits_structured_claims_bound_to_evidence():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
    )
    state["analysis"] = {
        "findings": ["差旅报销需要提交发票。", "差旅报销需要审批单。"],
        "missing_information": [],
        "confidence": 0.88,
    }
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["差旅报销需要什么？"],
        coverage="sufficient",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                title="差旅制度",
                content="差旅报销需要提交发票。",
                score=0.9,
            ),
            EvidenceItem(
                evidence_id="ev_2",
                chunk_id="chunk_2",
                document_id="doc_1",
                title="差旅制度",
                content="差旅报销需要审批单。",
                score=0.86,
            ),
        ],
    )

    result = synthesizer_node(state)
    draft = result["draft_answer"]

    assert draft["cited_evidence_ids"] == ["ev_1", "ev_2"]
    assert draft["claims"] == [
        {
            "claim_id": "claim_1",
            "text": "差旅报销需要提交发票。",
            "evidence_ids": ["ev_1"],
            "confidence": 0.9,
            "claim_type": "fact",
        },
        {
            "claim_id": "claim_2",
            "text": "差旅报销需要审批单。",
            "evidence_ids": ["ev_2"],
            "confidence": 0.86,
            "claim_type": "fact",
        },
    ]
    assert draft["missing_information"] == []
    assert draft["assumptions"] == []
    assert draft["response_policy"]["citation_style"] == "inline_title"


def test_synthesizer_uses_llm_for_content_rendering_without_changing_claims():
    llm_client = RecordingLlmClient(content="根据制度，差旅报销需要提交发票。")
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
    )
    state["analysis"] = {
        "findings": ["差旅报销需要提交发票。"],
        "missing_information": [],
        "confidence": 0.9,
    }
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["差旅报销需要什么？"],
        coverage="sufficient",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                title="差旅制度",
                content="差旅报销需要提交发票。",
                score=0.9,
            )
        ],
    )

    result = build_synthesizer_node(llm_client=llm_client)(state)
    draft = result["draft_answer"]

    assert draft["content"] == "根据制度，差旅报销需要提交发票。"
    assert draft["claims"] == [
        {
            "claim_id": "claim_1",
            "text": "差旅报销需要提交发票。",
            "evidence_ids": ["ev_1"],
            "confidence": 0.9,
            "claim_type": "fact",
        }
    ]
    assert draft["cited_evidence_ids"] == ["ev_1"]
    assert draft["response_policy"]["llm_rendering"] == "applied"
    assert llm_client.calls[0]["temperature"] == 0.0
    assert "claim_1" in llm_client.calls[0]["user_prompt"]


def test_synthesizer_preserves_missing_information_without_evidence():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="未知制度是什么？",
    )
    state["analysis"] = {
        "findings": [],
        "missing_information": ["没有可用证据"],
        "confidence": 0.0,
    }

    result = synthesizer_node(state)
    draft = result["draft_answer"]

    assert draft["claims"] == []
    assert draft["missing_information"] == ["没有可用证据"]
    assert draft["response_policy"]["uncertainty"] == "explicit"


def test_synthesizer_marks_partial_coverage_as_limited_answer():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
    )
    state["analysis"] = {
        "findings": ["差旅报销需要提交发票。"],
        "missing_information": ["审批单要求未确认"],
        "confidence": 0.9,
    }
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["差旅报销需要什么？"],
        coverage="partial",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                title="差旅制度",
                content="差旅报销需要提交发票。",
                score=0.9,
            )
        ],
    )

    result = synthesizer_node(state)
    draft = result["draft_answer"]

    assert draft["claims"][0]["claim_type"] == "fact"
    assert draft["missing_information"] == ["审批单要求未确认"]
    assert draft["confidence"] == 0.45
    assert draft["response_policy"]["coverage"] == "partial"


def test_verifier_rejects_partial_coverage_even_with_citation():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
    )
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["差旅报销需要什么？"],
        coverage="partial",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                title="差旅制度",
                content="差旅报销需要提交发票和审批单。",
                score=0.95,
            )
        ],
    )
    state["draft_answer"] = {
        "content": "根据《差旅制度》，差旅报销需要提交发票和审批单。",
        "cited_evidence_ids": ["ev_1"],
        "confidence": 0.95,
    }

    result = verifier_node(state)

    assert result["verification"]["verdict"] == "needs_revision"
    assert result["verification"]["required_action"] == "rerun_retrieval"


def test_verifier_rejects_draft_without_evidence_overlap():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
    )
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["差旅报销需要什么？"],
        coverage="sufficient",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                title="差旅制度",
                content="差旅报销需要提交发票和审批单。",
                score=0.95,
            )
        ],
    )
    state["draft_answer"] = {
        "content": "午餐补贴每人每天一千元。",
        "cited_evidence_ids": ["ev_1"],
        "confidence": 0.95,
    }

    result = verifier_node(state)

    assert result["verification"]["verdict"] == "needs_revision"
    assert result["verification"]["citation_errors"] == ["claim_not_supported_by_evidence"]


def test_verifier_requires_approval_for_uncited_empty_evidence_package():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
    )
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["差旅报销需要什么？"],
        coverage="none",
        items=[],
    )
    state["draft_answer"] = {
        "content": "当前知识库没有检索到足够证据，无法给出可靠结论。",
        "cited_evidence_ids": [],
        "confidence": 0.0,
    }

    result = verifier_node(state)

    assert result["verification"]["verdict"] == "needs_approval"
    assert result["verification"]["required_action"] == "request_approval"


def test_verifier_rejects_empty_draft_even_with_citation():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
    )
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["差旅报销需要什么？"],
        coverage="sufficient",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                title="差旅制度",
                content="差旅报销需要提交发票和审批单。",
                score=0.95,
            )
        ],
    )
    state["draft_answer"] = {
        "content": "",
        "cited_evidence_ids": ["ev_1"],
        "confidence": 0.95,
    }

    result = verifier_node(state)

    assert result["verification"]["verdict"] == "needs_revision"
    assert result["verification"]["citation_errors"] == ["claim_not_supported_by_evidence"]


def test_financial_verifier_rejects_buy_recommendation():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="贵州茅台能买吗",
    )
    state["intent"] = {"intent": "task_workflow", "domain": "a_share_equity_research"}
    state["plan"] = {
        "response_policy": {
            "citation_required": False,
            "disclaimer_required": True,
        }
    }
    state["draft_answer"] = {
        "content": "建议买入贵州茅台，长期稳赚。",
        "claims": [],
        "cited_evidence_ids": [],
        "missing_information": [],
        "assumptions": [],
        "confidence": 0.8,
        "response_policy": {"citation_required": False},
    }

    result = verifier_node(state)

    assert result["verification"]["verdict"] == "needs_revision"
    assert "investment_advice_boundary_violation" in result["verification"]["risk_flags"]
    assert result["verification"]["required_action"] == "revise_answer"


def test_financial_verifier_requires_disclaimer_for_equity_research():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下贵州茅台",
    )
    state["intent"] = {"intent": "task_workflow", "domain": "a_share_equity_research"}
    state["plan"] = {
        "response_policy": {
            "citation_required": False,
            "disclaimer_required": True,
        }
    }
    state["draft_answer"] = {
        "content": "贵州茅台主营白酒。",
        "claims": [],
        "cited_evidence_ids": [],
        "missing_information": [],
        "assumptions": [],
        "confidence": 0.8,
        "response_policy": {"citation_required": False},
    }

    result = verifier_node(state)

    assert result["verification"]["verdict"] == "needs_revision"
    assert "missing_financial_disclaimer" in result["verification"]["risk_flags"]


def test_approval_coordinator_marks_uncited_draft_as_pending():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    state["draft_answer"] = {
        "content": "当前知识库没有检索到足够证据，无法给出可靠结论。",
        "cited_evidence_ids": [],
        "confidence": 0.0,
    }

    state = verifier_node(state)
    state = approval_coordinator_node(state)

    assert state["verification"]["verdict"] == "needs_approval"
    assert state["approval_status"].required is True
    assert state["approval_status"].status == "pending"
