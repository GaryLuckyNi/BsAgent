import json

from app.intent.catalog import BusinessIntentCatalog
import pytest

from app.intent.router import HybridIntentRouter, IntentClassificationError
from app.intent.schemas import IntentDecisionModel
from app.graph.nodes.intent_router import build_intent_router_node
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult
from app.skills.definitions import SkillCard


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


def test_intent_router_node_passes_safe_image_summaries_to_classifier():
    class RecordingRouter:
        def __init__(self):
            self.context = None

        def route(self, *, message, tenant_id, user_id, context):
            del message, tenant_id, user_id
            self.context = context
            return IntentDecisionModel(
                intent="task_workflow",
                domain="a_share_equity_research",
                sub_intent="analyze_financial_image",
                risk_level="medium",
                confidence=0.9,
            )

    router = RecordingRouter()
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="这张图说明了什么？",
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
            "summary": "营业收入同比增长。",
            "extracted_text": "不得进入意图分类上下文的 OCR 原文",
            "financial_metrics": [],
            "chart_observations": [],
            "entities": ["示例公司"],
            "risk_flags": ["仅 OCR 可用"],
            "confidence": 0.72,
            "truncated": True,
        }
    ]

    build_intent_router_node(router=router)(state)

    assert router.context == {
        "image_analyses": [
            {
                "analysis_id": "analysis-1",
                "document_type": "financial_statement",
                "summary": "营业收入同比增长。",
                "entities": ["示例公司"],
                "warnings": ["仅 OCR 可用"],
                "confidence": 0.72,
            }
        ]
    }
    assert "OCR 原文" not in str(router.context)


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


def test_llm_intent_classifier_receives_tenant_skill_cards():
    llm_client = FakeLlmClient(
        """
        {
          "intent": "task_workflow",
          "domain": "a_share_equity_research",
          "sub_intent": "company_snapshot",
          "candidate_skill": "company_snapshot",
          "candidate_tool": null,
          "risk_level": "medium",
          "confidence": 0.91,
          "needs_clarification": false,
          "reason_codes": ["classifier_selected_skill"]
        }
        """
    )
    requested_tenants: list[str] = []
    router = HybridIntentRouter(
        catalog=BusinessIntentCatalog.load_default(),
        llm_client=llm_client,
        available_skills_provider=lambda tenant_id: (
            requested_tenants.append(tenant_id)
            or [
                SkillCard(
                    name="company_snapshot",
                    version="2.0.0",
                    summary="生成 A 股公司的基本面与市场数据速览",
                    supported_intents=["task_workflow"],
                    tags=["a_share_equity_research", "company_snapshot"],
                    risk_level="medium",
                    definition_hash="skill-company-snapshot-v2",
                )
            ]
        ),
    )

    decision = router.route(
        message="帮我梳理这家公司的竞争格局",
        tenant_id="tenant_skill_cards",
        user_id="user_1",
    )

    assert decision.candidate_skill == "company_snapshot"
    assert requested_tenants == ["tenant_skill_cards"]
    payload = json.loads(llm_client.calls[0]["user_prompt"])
    assert payload["available_skills"] == [
        {
            "name": "company_snapshot",
            "version": "2.0.0",
            "summary": "生成 A 股公司的基本面与市场数据速览",
            "supported_intents": ["task_workflow"],
            "tags": ["a_share_equity_research", "company_snapshot"],
            "risk_level": "medium",
        }
    ]


def test_llm_intent_classifier_does_not_receive_tool_catalog():
    llm_client = FakeLlmClient(
        """
        {
          "intent": "task_workflow",
          "domain": "a_share_equity_research",
          "sub_intent": "company_snapshot",
          "candidate_skill": "company_snapshot",
          "candidate_tool": null,
          "risk_level": "medium",
          "confidence": 0.91,
          "needs_clarification": false,
          "reason_codes": ["classifier_selected_skill"]
        }
        """
    )
    router = HybridIntentRouter(
        catalog=BusinessIntentCatalog.load_default(),
        llm_client=llm_client,
        available_skills_provider=lambda _tenant_id: [
            SkillCard(
                name="company_snapshot",
                version="2.0.0",
                summary="生成 A 股公司快照",
                supported_intents=["task_workflow"],
                tags=["a_share_equity_research"],
                risk_level="medium",
                definition_hash="skill-company-snapshot-v2",
            )
        ],
    )

    router.route(
        message="帮我梳理这家公司的竞争格局",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    payload = json.loads(llm_client.calls[0]["user_prompt"])
    assert "available_tools" not in payload
    assert "tool_schema" not in llm_client.calls[0]["user_prompt"]
    assert payload["available_skills"][0]["name"] == "company_snapshot"
    assert payload["required_schema"]["candidate_tool"] == "null"
    assert "candidate_tool 必须输出 null" in llm_client.calls[0]["system_prompt"]


def test_llm_intent_classifier_degrades_to_empty_skill_cards_when_registry_unavailable():
    llm_client = FakeLlmClient(
        """
        {
          "intent": "unsupported",
          "domain": "out_of_scope",
          "sub_intent": "software_development",
          "candidate_skill": null,
          "candidate_tool": null,
          "risk_level": "low",
          "confidence": 0.91,
          "needs_clarification": false,
          "reason_codes": ["classifier_selected_unsupported"]
        }
        """
    )
    router = HybridIntentRouter(
        catalog=BusinessIntentCatalog.load_default(),
        llm_client=llm_client,
        available_skills_provider=lambda _tenant_id: (_ for _ in ()).throw(
            RuntimeError("registry unavailable")
        ),
    )

    decision = router.route(
        message="帮我写一个 Vue 组件",
        tenant_id="tenant_unavailable",
        user_id="user_1",
    )

    assert decision.intent == "unsupported"
    payload = json.loads(llm_client.calls[0]["user_prompt"])
    assert payload["available_skills"] == []


def test_llm_intent_classifier_receives_balanced_research_boundary_policy():
    llm_client = FakeLlmClient(
        """
        {
          "intent": "unsupported",
          "domain": "out_of_scope",
          "sub_intent": "software_development",
          "candidate_skill": null,
          "candidate_tool": null,
          "risk_level": "low",
          "confidence": 0.94,
          "needs_clarification": false,
          "reason_codes": ["outside_research_scope"]
        }
        """
    )
    router = HybridIntentRouter(
        catalog=BusinessIntentCatalog.load_default(),
        llm_client=llm_client,
    )

    decision = router.route(
        message="帮我写一个 Vue 表格组件",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert decision.intent == "unsupported"
    assert decision.domain == "out_of_scope"
    system_prompt = llm_client.calls[0]["system_prompt"]
    assert "投研优先" in system_prompt
    assert "投研无关" in system_prompt


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
