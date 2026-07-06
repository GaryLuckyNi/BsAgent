from pydantic import BaseModel

from app.graph.nodes.approval_coordinator import (
    approval_coordinator_node,
    build_approval_coordinator_node,
)
from app.graph.nodes.analysis_agent import analysis_agent_node
from app.graph.nodes.intent_router import intent_router_node
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.nodes.skill_agent import build_skill_agent_node, skill_agent_node
from app.graph.nodes.synthesizer import build_synthesizer_node, synthesizer_node
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.nodes.verifier import verifier_node
from app.graph.state import create_initial_state
from app.schemas.approvals import ApprovalRequest
from app.schemas.evidence import EvidenceItem, EvidencePackage
from app.schemas.memory import MemoryCandidate, MemoryContext
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult
from app.schemas.tools import ToolSpec
from app.llm.schemas import LlmCompletionResult
from app.skills.base import ApprovalPolicy, Skill, SkillOutput, SkillSpec
from app.skills.registry import SkillRegistry
from app.tools.base import Tool
from app.tools.registry import ToolRegistry


class RecordingExperienceStore:
    def __init__(self) -> None:
        self.payloads = []

    def save(self, payload) -> None:
        self.payloads.append(payload)


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


class ToolPlanningLlmClient(RecordingLlmClient):
    def __init__(self) -> None:
        super().__init__(
            """
            {
              "tool_name": "mcp.mock.calculate_sum",
              "tool_version": "mcp-stdio-v1",
              "arguments": {"numbers": [1, 2, 3]},
              "purpose": "计算用户给出的数字总和",
              "approval_required": false
            }
            """
        )


def test_intent_router_marks_policy_question_as_knowledge_qa():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="公司报销制度是什么？",
    )

    result = intent_router_node(state)

    assert result["intent"]["intent"] == "knowledge_qa"
    assert result["intent"]["domain"] == "finance_policy"
    assert result["intent"]["sub_intent"] == "policy_lookup"
    assert result["intent"]["candidate_skill"] == "policy_qa"
    assert result["intent"]["confidence"] >= 0.7
    assert result["intent"]["next_node"] == "planner_agent"


def test_planner_agent_builds_execution_plan_from_intent():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
    )
    state = intent_router_node(state)

    result = build_planner_agent_node(available_tools=[])(state)

    assert result["plan"]["objective"] == "回答用户的知识问答请求"
    assert result["plan"]["selected_skill"]["name"] == "policy_qa"
    assert result["plan"]["execution_steps"] == [
        "select_skill",
        "retrieve_evidence",
        "analyze_evidence",
        "synthesize_answer",
        "verify_answer",
    ]
    assert result["plan"]["retrieval"]["required"] is True
    assert result["plan"]["tool_policy"]["allowed_tools"] == ["rag_search"]
    assert result["plan"]["risk_controls"]["risk_level"] == "medium"


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


def test_planner_agent_builds_dynamic_tool_plan_from_candidate_tool():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="1+2+3=?",
    )
    state["intent"] = {
        "intent": "tool_action",
        "domain": "mcp_math",
        "sub_intent": "calculate_sum",
        "risk_level": "low",
        "needs_clarification": False,
        "candidate_skill": None,
        "candidate_tool": "mcp.mock.calculate_sum",
        "confidence": 0.86,
        "reason_codes": ["business_profile:mcp_math.calculate_sum"],
        "next_node": "planner_agent",
    }

    result = build_planner_agent_node(
        llm_client=ToolPlanningLlmClient(),
        available_tools=[
            ToolSpec(
                name="mcp.mock.calculate_sum",
                version="mcp-stdio-v1",
                description="计算一组数字的总和",
                input_schema={
                    "type": "object",
                    "properties": {"numbers": {"type": "array", "items": {"type": "number"}}},
                    "required": ["numbers"],
                },
                risk_level="low",
            )
        ],
    )(state)

    assert result["plan"]["execution_mode"] == "tool_execution"
    assert result["plan"]["retrieval"]["required"] is False
    assert result["plan"]["tool_policy"]["allowed_tools"] == ["mcp.mock.calculate_sum"]
    assert len(result["plan"]["requested_tools"]) == 1
    request = result["plan"]["requested_tools"][0]
    assert request.tool_name == "mcp.mock.calculate_sum"
    assert request.arguments == {"numbers": [1, 2, 3]}


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
    state["memory_context"] = MemoryContext(entries=["用户偏好：所有回答用中文"])
    state["plan"] = {
        "selected_skill": {"name": "recording_skill"},
        "execution_steps": ["select_skill", "retrieve_evidence"],
    }

    result = build_skill_agent_node(registry=registry)(state)

    assert result["selected_skill"]["skill_name"] == "recording_skill"
    assert result["selected_skill"]["skill_version"] == "2.0.0"
    assert result["selected_skill"]["status"] == "needs_retrieval"
    assert result["plan"]["execution_steps"] == ["select_skill", "retrieve_evidence"]
    assert result["plan"]["skill_output"]["skill_name"] == "recording_skill"
    assert result["plan"]["requested_tools"][0].tool_name == "rag_search"
    assert skill.calls[0]["memory_context"].entries == ["用户偏好：所有回答用中文"]


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

    result = build_tool_agent_node(registry_factory=registry_factory)(state)

    assert result["tool_calls"][1].request.arguments["symbol"] == "600519"
    assert result["tool_calls"][1].result.result["symbol"] == "600519"


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
        memory_context=None,
    )
    request_names = [request.tool_name for request in output.requested_tools]

    assert output.skill_name == "company_snapshot"
    assert "mcp.mock.stock.resolve_company" in request_names
    assert "mcp.mock.stock.company_profile" in request_names
    assert "mcp.mock.stock.market_snapshot" in request_names
    assert "rag_search" in request_names
    assert output.requested_tools[1].arguments["symbol"] == "$resolved_symbol"


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
        memory_context=None,
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
        memory_context=None,
    )
    half_year_output = skill.run_validated(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析宁德时代 2024 半年报",
        context={"intent": {"sub_intent": "financial_report_analysis"}},
        evidence=None,
        memory_context=None,
    )

    assert annual_output.requested_tools[1].arguments["period"] == "2024"
    assert half_year_output.requested_tools[1].arguments["period"] == "2024H1"


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


def test_analysis_agent_collects_successful_stock_tool_results():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下贵州茅台",
    )
    state["tool_calls"] = [
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.mock.stock.company_profile",
                arguments={"symbol": "600519.SH"},
                purpose="获取公司基础资料",
            ),
            result=ToolCallResult(
                tool_call_id="tool_1",
                tool_name="mcp.mock.stock.company_profile",
                status="success",
                result={
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "industry": "食品饮料",
                    "main_business": "茅台酒及系列酒的生产与销售。",
                    "source_provider": "fixture",
                    "as_of_date": "2025-04-03",
                    "warnings": [],
                },
                trace_id="trace_1",
            ),
        )
    ]

    result = analysis_agent_node(state)

    assert (
        result["analysis"]["structured_tool_results"][0]["tool_name"]
        == "mcp.mock.stock.company_profile"
    )
    assert result["analysis"]["structured_tool_results"][0]["result"]["symbol"] == "600519.SH"


def test_synthesizer_adds_financial_disclaimer_for_equity_research_answer():
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
    }
    state["plan"] = {
        "execution_mode": "skill_execution",
        "response_policy": {
            "response_type": "equity_research_company_snapshot",
            "citation_required": False,
            "disclaimer_required": True,
        },
    }
    state["analysis"] = {
        "findings": ["贵州茅台主营茅台酒及系列酒。"],
        "structured_tool_results": [
            {
                "tool_name": "mcp.mock.stock.company_profile",
                "result": {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "industry": "食品饮料",
                    "main_business": "茅台酒及系列酒的生产与销售。",
                },
            }
        ],
        "missing_information": [],
        "confidence": 0.8,
    }

    result = synthesizer_node(state)

    assert "公司概览" in result["draft_answer"]["content"]
    assert "贵州茅台" in result["draft_answer"]["content"]
    assert "不构成投资建议" in result["draft_answer"]["content"]


def _configured_skill(*, allowed_tools: list[str], risk_level: str = "medium"):
    from app.skills.configured import ConfiguredSkill
    from app.skills.definitions import SkillDefinition, SkillExecutionGuide

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


def test_synthesizer_applies_chinese_response_preference_from_memory_context():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="What documents are required for reimbursement?",
    )
    state["memory_context"] = MemoryContext(
        entries=["用户偏好：所有回答用中文"],
        sources=["experience/memcand_1"],
    )
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["reimbursement documents"],
        coverage="sufficient",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                title="Reimbursement Policy",
                content="Submit invoices and approval forms.",
                score=0.95,
            )
        ],
    )

    result = synthesizer_node(state)

    assert result["draft_answer"]["content"].startswith("按你的中文偏好回答：")


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
                tool_name="mcp.mock.calculate_sum",
                tool_version="mcp-stdio-v1",
                arguments={"numbers": list(range(1, 51))},
                purpose="计算 1 到 50 的总和",
            ),
            result={
                "tool_call_id": "tool_1",
                "tool_name": "mcp.mock.calculate_sum",
                "status": "success",
                "result": {"total": 1275, "count": 50},
                "trace_id": "trace_1",
            },
        )
    ]

    result = synthesizer_node(state)

    assert "mcp.mock.calculate_sum" not in result["draft_answer"]["content"]
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
                tool_name="mcp.mock.calculate_sum",
                tool_version="mcp-stdio-v1",
                arguments={"numbers": [1, 2, 3]},
                purpose="计算用户给出的数字总和",
            ),
            result={
                "tool_call_id": "tool_1",
                "tool_name": "mcp.mock.calculate_sum",
                "status": "success",
                "result": {"total": 6, "count": 3},
                "trace_id": "trace_1",
            },
        )
    ]

    result = build_synthesizer_node(llm_client=llm_client)(state)

    assert result["draft_answer"]["content"] == "1+2+3 的结果是 6。"
    assert "mcp.mock.calculate_sum" not in result["draft_answer"]["content"]
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


def test_skill_agent_falls_back_to_policy_qa_for_unknown_candidate():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    state["intent"] = {
        "intent": "knowledge_qa",
        "risk_level": "medium",
        "needs_clarification": False,
        "candidate_skill": "unknown_skill",
        "reason_codes": [],
        "next_node": "skill_agent",
    }

    result = skill_agent_node(state)

    assert result["selected_skill"]["skill_name"] == "policy_qa"
    assert result["selected_skill"]["skill_version"] == "1.0.0"


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


def test_approval_coordinator_commits_approved_memory_candidate_to_experience_store():
    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="记住我的语言偏好，所有的文档用中文",
    )
    state["memory_candidates"] = [
        MemoryCandidate(
            candidate_id="memcand_1",
            memory_type="user_preference",
            content="所有的文档用中文",
            confidence=0.9,
            approval_status="approved",
            sensitivity="low",
            source="user_explicit_preference",
        )
    ]
    state["approval_requests"] = [
        ApprovalRequest(
            approval_id="approval_1",
            run_id=state["request_id"],
            approval_type="memory_write",
            risk_level="low",
            payload={"candidate_id": "memcand_1"},
            status="approved",
        )
    ]
    store = RecordingExperienceStore()

    result = build_approval_coordinator_node(experience_store=store)(state)

    assert result["memory_candidates"][0].approval_status == "approved"
    assert store.payloads[0].candidate_id == "memcand_1"
    assert store.payloads[0].content == "所有的文档用中文"
    assert result["approval_requests"][0].payload["experience_committed"] is True


def test_approval_coordinator_does_not_commit_memory_candidate_twice():
    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="记住我的语言偏好，所有的文档用中文",
    )
    state["memory_candidates"] = [
        MemoryCandidate(
            candidate_id="memcand_1",
            memory_type="user_preference",
            content="所有的文档用中文",
            confidence=0.9,
            approval_status="approved",
            sensitivity="low",
            source="user_explicit_preference",
        )
    ]
    state["approval_requests"] = [
        ApprovalRequest(
            approval_id="approval_1",
            run_id=state["request_id"],
            approval_type="memory_write",
            risk_level="low",
            payload={"candidate_id": "memcand_1", "experience_committed": True},
            status="approved",
        )
    ]
    store = RecordingExperienceStore()

    build_approval_coordinator_node(experience_store=store)(state)

    assert store.payloads == []
