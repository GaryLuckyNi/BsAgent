from app.intent.catalog import BusinessIntentCatalog
import pytest

from app.intent.router import HybridIntentRouter, IntentClassificationError
from app.intent.schemas import IntentDecisionModel
from app.llm.schemas import LlmCompletionResult
from app.schemas.tools import ToolSpec


class FakeLlmClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(
            content=self.content,
            model="fake-intent-model",
            provider="fake",
        )


def test_business_intent_catalog_loads_policy_qa_profile_from_default_config():
    catalog = BusinessIntentCatalog.load_default()

    profile = catalog.match_by_keywords("差旅报销需要提交什么材料？")

    assert profile is not None
    assert profile.intent == "knowledge_qa"
    assert profile.domain == "finance_policy"
    assert profile.sub_intent == "policy_lookup"
    assert profile.candidate_skill == "policy_qa"


def test_hybrid_intent_router_uses_business_profile_for_policy_question():
    router = HybridIntentRouter(catalog=BusinessIntentCatalog.load_default())

    decision = router.route(
        message="公司差旅报销制度是什么？",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision.intent == "knowledge_qa"
    assert decision.domain == "finance_policy"
    assert decision.sub_intent == "policy_lookup"
    assert decision.candidate_skill == "policy_qa"
    assert decision.confidence >= 0.7
    assert "business_profile:finance_policy.policy_lookup" in decision.reason_codes


def test_hybrid_intent_router_applies_high_risk_floor_from_rules():
    router = HybridIntentRouter(catalog=BusinessIntentCatalog.load_default())

    decision = router.route(
        message="帮我删除这条客户记录",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision.intent == "tool_action"
    assert decision.risk_level == "high"
    assert "rule_high_risk_action" in decision.reason_codes


def test_hybrid_intent_router_detects_calculate_sum_as_tool_action():
    router = HybridIntentRouter(catalog=BusinessIntentCatalog.load_default())

    decision = router.route(
        message="计算1加到50",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision.intent == "tool_action"
    assert decision.domain == "mcp_math"
    assert decision.sub_intent == "calculate_sum"
    assert decision.candidate_tool == "mcp.mock.calculate_sum"
    assert decision.confidence >= 0.7


def test_hybrid_intent_router_detects_plus_expression_sum_as_tool_action():
    llm_client = FakeLlmClient(
        """
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
        """
    )
    router = HybridIntentRouter(
        catalog=BusinessIntentCatalog.load_default(),
        llm_client=llm_client,
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
    )

    decision = router.route(
        message="你可以帮我算一下1+2+3+4+5+6+7+8+9+10等于多少吗",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision.intent == "tool_action"
    assert decision.domain == "mcp_math"
    assert decision.sub_intent == "calculate_sum"
    assert decision.candidate_tool == "mcp.mock.calculate_sum"
    assert decision.confidence >= 0.7
    assert llm_client.calls
    assert "available_tools" in llm_client.calls[0]["user_prompt"]
    assert "mcp.mock.calculate_sum" in llm_client.calls[0]["user_prompt"]


def test_hybrid_intent_router_sends_short_sum_expression_to_llm_classifier():
    llm_client = FakeLlmClient(
        """
        {
          "intent": "tool_action",
          "domain": "mcp_math",
          "sub_intent": "calculate_sum",
          "candidate_skill": null,
          "candidate_tool": "mcp.mock.calculate_sum",
          "risk_level": "low",
          "confidence": 0.93,
          "needs_clarification": false,
          "reason_codes": ["classifier_selected_sum_tool"]
        }
        """
    )
    router = HybridIntentRouter(
        catalog=BusinessIntentCatalog.load_default(),
        llm_client=llm_client,
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
    )

    decision = router.route(
        message="1+2+3=?",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision.intent == "tool_action"
    assert decision.candidate_tool == "mcp.mock.calculate_sum"
    assert llm_client.calls


def test_hybrid_intent_router_can_use_llm_classification_for_business_intent():
    llm_client = FakeLlmClient(
        """
        {
          "intent": "tool_action",
          "domain": "crm",
          "sub_intent": "update_customer_status",
          "candidate_skill": "crm_update",
          "candidate_tool": null,
          "risk_level": "high",
          "confidence": 0.86,
          "needs_clarification": false,
          "reason_codes": ["classifier_selected_crm_update"]
        }
        """
    )
    router = HybridIntentRouter(
        catalog=BusinessIntentCatalog.load_default(),
        llm_client=llm_client,
    )

    decision = router.route(
        message="把客户 A 的状态改成已成交",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision == IntentDecisionModel(
        intent="tool_action",
        domain="crm",
        sub_intent="update_customer_status",
        candidate_skill="crm_update",
        candidate_tool=None,
        risk_level="high",
        confidence=0.86,
        needs_clarification=False,
        reason_codes=["classifier_selected_crm_update"],
        next_node="planner_agent",
    )
    assert llm_client.calls


def test_hybrid_intent_router_uses_business_rules_before_invalid_llm_output():
    llm_client = FakeLlmClient("不是 JSON")
    router = HybridIntentRouter(
        catalog=BusinessIntentCatalog.load_default(),
        llm_client=llm_client,
    )

    decision = router.route(
        message="差旅报销需要什么？",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision.intent == "knowledge_qa"
    assert decision.domain == "finance_policy"
    assert decision.sub_intent == "policy_lookup"
    assert decision.candidate_skill == "policy_qa"
    assert not llm_client.calls


def test_hybrid_intent_router_rejects_unknown_intent_without_llm_classifier():
    router = HybridIntentRouter(catalog=BusinessIntentCatalog.load_default())

    with pytest.raises(IntentClassificationError) as exc_info:
        router.route(
            message="1+2+3=?",
            tenant_id="tenant_1",
            user_id="user_1",
        )

    assert exc_info.value.code == "intent_classifier_not_configured"
    assert exc_info.value.http_status == 503


def test_hybrid_intent_router_detects_a_share_company_snapshot():
    router = HybridIntentRouter(catalog=BusinessIntentCatalog.load_default())

    decision = router.route(
        message="分析一下贵州茅台",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision.intent == "task_workflow"
    assert decision.domain == "a_share_equity_research"
    assert decision.sub_intent == "company_snapshot"
    assert decision.candidate_skill == "company_snapshot"
    assert decision.risk_level == "medium"


def test_hybrid_intent_router_detects_a_share_financial_report_analysis():
    router = HybridIntentRouter(catalog=BusinessIntentCatalog.load_default())

    decision = router.route(
        message="分析宁德时代 2024 年报",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision.intent == "task_workflow"
    assert decision.domain == "a_share_equity_research"
    assert decision.sub_intent == "financial_report_analysis"
    assert decision.candidate_skill == "financial_report_analysis"


def test_hybrid_intent_router_prefers_financial_report_over_company_snapshot_when_question_mentions_report():
    router = HybridIntentRouter(catalog=BusinessIntentCatalog.load_default())

    decision = router.route(
        message="分析一下宁德时代 2024 年报",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision.sub_intent == "financial_report_analysis"
    assert decision.candidate_skill == "financial_report_analysis"


def test_hybrid_intent_router_detects_a_share_risk_scan():
    router = HybridIntentRouter(catalog=BusinessIntentCatalog.load_default())

    decision = router.route(
        message="看看隆基绿能最近有什么风险",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision.intent == "task_workflow"
    assert decision.domain == "a_share_equity_research"
    assert decision.sub_intent == "risk_scan"
    assert decision.candidate_skill == "risk_scan"


def test_hybrid_intent_router_prefers_risk_scan_over_company_snapshot_when_question_mentions_risk():
    router = HybridIntentRouter(catalog=BusinessIntentCatalog.load_default())

    decision = router.route(
        message="分析一下宁德时代最近有什么风险",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision.sub_intent == "risk_scan"
    assert decision.candidate_skill == "risk_scan"
