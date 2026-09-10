import json

import pytest

from app.intent.router import HybridIntentRouter, IntentClassificationError
from app.intent.schemas import (
    IntentDecisionModel,
    IntentRoutingResult,
)
from app.graph.nodes import intent_router as intent_router_module
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


def _routing_content(
    *,
    intent: str,
    domain: str | None,
    sub_intent: str | None,
    candidate_skill: str | None = None,
    risk_level: str = "medium",
    confidence: float = 0.9,
    reason_codes: list[str] | None = None,
    task_brief: dict | None = None,
) -> str:
    return json.dumps(
        {
            "decision": {
                "intent": intent,
                "domain": domain,
                "sub_intent": sub_intent,
                "candidate_skill": candidate_skill,
                "candidate_tool": None,
                "risk_level": risk_level,
                "confidence": confidence,
                "needs_clarification": False,
                "reason_codes": reason_codes or [],
            },
            "task_brief": task_brief,
        },
        ensure_ascii=False,
    )


def _equity_task_brief(company: str) -> dict:
    return {
        "user_goal": f"分析{company}的投资价值",
        "entities": [{"type": "company", "mention": company}],
        "analysis_questions": ["基本面与估值是否匹配"],
        "search_keywords": [company],
        "freshness_required": True,
        "web_search_required": True,
        "web_search_reason": "需要近期公开信息",
        "rag_required": False,
        "time_horizon": None,
        "output_expectation": "提供平衡的投研分析",
        "constraints": ["不生成个性化买卖指令"],
        "needs_clarification": False,
    }


def test_business_intent_catalog_has_no_local_file_loaders(
    business_intent_catalog,
):
    removed_loader_names = ("load_" + "default", "load_from_" + "path")

    assert all(
        not hasattr(type(business_intent_catalog), name)
        for name in removed_loader_names
    )


def test_hybrid_intent_router_requires_explicit_catalog():
    with pytest.raises(TypeError, match="catalog"):
        HybridIntentRouter()

    with pytest.raises(TypeError, match="catalog"):
        HybridIntentRouter(catalog=None)


def test_intent_router_builders_always_require_catalog():
    with pytest.raises(TypeError, match="catalog"):
        build_intent_router_node()

    with pytest.raises(TypeError, match="llm_client"):
        build_intent_router_node(catalog=object())

    with pytest.raises(TypeError, match="available_skills_provider"):
        build_intent_router_node(catalog=object(), llm_client=None)

    with pytest.raises(TypeError, match="catalog"):
        build_intent_router_node(
            catalog=None,
            llm_client=None,
            available_skills_provider=None,
        )


def test_intent_router_module_has_no_implicit_compatibility_node():
    assert not hasattr(intent_router_module, "intent_router_node")


def test_intent_router_node_passes_safe_image_summaries_to_classifier(
    business_intent_catalog,
):
    llm_client = FakeLlmClient(
        _routing_content(
            intent="task_workflow",
            domain="a_share_equity_research",
            sub_intent="analyze_financial_image",
            task_brief={
                "user_goal": "分析图片中的财务信息",
                "entities": [{"type": "document", "mention": "用户上传图片"}],
                "analysis_questions": [],
                "search_keywords": [],
                "freshness_required": False,
                "web_search_required": False,
                "web_search_reason": None,
                "rag_required": False,
                "time_horizon": None,
                "output_expectation": "解释图片中的关键财务信息",
                "constraints": [],
                "needs_clarification": False,
            },
        )
    )
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

    build_intent_router_node(
        catalog=business_intent_catalog,
        llm_client=llm_client,
        available_skills_provider=None,
    )(state)

    payload = json.loads(llm_client.calls[0]["user_prompt"])
    assert payload["context"] == {
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
    assert "OCR 原文" not in str(payload["context"])
    assert state["task_brief"]["user_goal"] == "分析图片中的财务信息"
    assert "task_brief" not in state["intent"]


def test_hybrid_intent_router_applies_high_risk_floor_from_rules(
    business_intent_catalog,
):
    router = HybridIntentRouter(catalog=business_intent_catalog)

    result = router.route(
        message="帮我删除这条客户记录",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert result.decision.intent == "tool_action"
    assert result.decision.risk_level == "high"
    assert "rule_high_risk_action" in result.decision.reason_codes
    assert result.task_brief is None


def test_hybrid_intent_router_can_use_llm_classification_for_business_intent(
    business_intent_catalog,
):
    llm_client = FakeLlmClient(
        _routing_content(
            intent="tool_action",
            domain="crm",
            sub_intent="update_customer_status",
            candidate_skill="crm_update",
            risk_level="high",
            confidence=0.86,
            reason_codes=["classifier_selected_crm_update"],
        )
    )
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=llm_client,
    )

    result = router.route(
        message="把客户 A 的状态改成已成交",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert result == IntentRoutingResult(
        decision=IntentDecisionModel(
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
        ),
        task_brief=None,
    )
    assert llm_client.calls


def test_unmatched_equity_question_returns_open_task_brief(
    business_intent_catalog,
):
    llm_client = FakeLlmClient(
        _routing_content(
            intent="task_workflow",
            domain="a_share_equity_research",
            sub_intent=None,
            confidence=0.86,
            reason_codes=["open_equity_research_task"],
            task_brief={
                **_equity_task_brief("海能达"),
                "user_goal": "评估海能达当前是否具备投资吸引力",
                "analysis_questions": [
                    "当前估值处于什么水平",
                    "盈利和现金流质量是否改善",
                    "存在哪些催化因素与下行风险",
                ],
                "search_keywords": ["海能达", "最新公告", "近期经营动态"],
            },
        )
    )
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=llm_client,
    )

    result = router.route(
        message="海能达现在是否适合买入？",
        tenant_id="t1",
        user_id="u1",
    )

    assert result.decision.intent == "task_workflow"
    assert result.decision.domain == "a_share_equity_research"
    assert result.decision.sub_intent is None
    assert result.task_brief is not None
    assert result.task_brief.entities[0].mention == "海能达"
    assert result.task_brief.freshness_required is True
    assert result.task_brief.web_search_required is True
    assert "海能达" in result.task_brief.search_keywords
    assert len(llm_client.calls) == 1
    payload = json.loads(llm_client.calls[0]["user_prompt"])
    assert payload["matched_profile"] is None
    assert set(payload["required_schema"]) == {"decision", "task_brief"}


def test_llm_intent_classifier_receives_tenant_skill_cards(
    business_intent_catalog,
):
    llm_client = FakeLlmClient(
        _routing_content(
            intent="task_workflow",
            domain="a_share_equity_research",
            sub_intent="company_snapshot",
            candidate_skill="company_snapshot",
            confidence=0.91,
            reason_codes=["classifier_selected_skill"],
            task_brief=_equity_task_brief("目标公司"),
        )
    )
    requested_tenants: list[str] = []
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
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

    result = router.route(
        message="帮我梳理这家公司的竞争格局",
        tenant_id="tenant_skill_cards",
        user_id="user_1",
    )

    assert result.decision.candidate_skill == "company_snapshot"
    assert result.task_brief is not None
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


def test_llm_intent_classifier_does_not_receive_tool_catalog(
    business_intent_catalog,
):
    llm_client = FakeLlmClient(
        _routing_content(
            intent="task_workflow",
            domain="a_share_equity_research",
            sub_intent="company_snapshot",
            candidate_skill="company_snapshot",
            confidence=0.91,
            reason_codes=["classifier_selected_skill"],
            task_brief=_equity_task_brief("目标公司"),
        )
    )
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
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
    assert payload["required_schema"]["decision"]["candidate_tool"] == "null"
    assert "candidate_tool 必须输出 null" in llm_client.calls[0]["system_prompt"]


def test_llm_intent_classifier_degrades_to_empty_skill_cards_when_registry_unavailable(
    business_intent_catalog,
):
    llm_client = FakeLlmClient(
        _routing_content(
            intent="unsupported",
            domain="out_of_scope",
            sub_intent="software_development",
            risk_level="low",
            confidence=0.91,
            reason_codes=["classifier_selected_unsupported"],
        )
    )
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=llm_client,
        available_skills_provider=lambda _tenant_id: (_ for _ in ()).throw(
            RuntimeError("registry unavailable")
        ),
    )

    result = router.route(
        message="帮我写一个 Vue 组件",
        tenant_id="tenant_unavailable",
        user_id="user_1",
    )

    assert result.decision.intent == "unsupported"
    assert result.task_brief is None
    payload = json.loads(llm_client.calls[0]["user_prompt"])
    assert payload["available_skills"] == []


def test_llm_intent_classifier_normalizes_only_non_workflow_flat_payloads(
    business_intent_catalog,
):
    flat_conversation = json.dumps(
        {
            "intent": "conversation",
            "domain": "general",
            "sub_intent": None,
            "candidate_skill": None,
            "candidate_tool": None,
            "risk_level": "low",
            "confidence": 0.9,
            "needs_clarification": False,
            "reason_codes": ["legacy_flat_payload"],
        }
    )
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=FakeLlmClient(flat_conversation),
    )

    result = router.route(
        message="讲个笑话",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert result.decision.intent == "conversation"
    assert result.task_brief is None

    flat_workflow = json.dumps(
        {
            "intent": "task_workflow",
            "domain": "a_share_equity_research",
            "sub_intent": None,
            "candidate_skill": None,
            "candidate_tool": None,
            "risk_level": "medium",
            "confidence": 0.9,
            "needs_clarification": False,
            "reason_codes": ["legacy_flat_payload"],
        }
    )
    strict_router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=FakeLlmClient(flat_workflow),
    )

    with pytest.raises(IntentClassificationError):
        strict_router.route(
            message="海能达是否值得关注",
            tenant_id="tenant_1",
            user_id="user_1",
        )


def test_llm_intent_classifier_receives_balanced_research_boundary_policy(
    business_intent_catalog,
):
    llm_client = FakeLlmClient(
        _routing_content(
            intent="unsupported",
            domain="out_of_scope",
            sub_intent="software_development",
            risk_level="low",
            confidence=0.94,
            reason_codes=["outside_research_scope"],
        )
    )
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=llm_client,
    )

    result = router.route(
        message="帮我写一个 Vue 表格组件",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert result.decision.intent == "unsupported"
    assert result.decision.domain == "out_of_scope"
    assert result.task_brief is None
    system_prompt = llm_client.calls[0]["system_prompt"]
    assert "投研优先" in system_prompt
    assert "投研无关" in system_prompt


def test_hybrid_intent_router_rejects_unknown_intent_without_llm_classifier(
    business_intent_catalog,
):
    router = HybridIntentRouter(catalog=business_intent_catalog)

    with pytest.raises(IntentClassificationError) as exc_info:
        router.route(
            message="1+2+3=?",
            tenant_id="tenant_1",
            user_id="user_1",
        )

    assert exc_info.value.code == "intent_classifier_not_configured"
    assert exc_info.value.http_status == 503


def test_hybrid_intent_router_detects_a_share_company_snapshot(
    business_intent_catalog,
):
    llm_client = FakeLlmClient(
        _routing_content(
            intent="task_workflow",
            domain="a_share_equity_research",
            sub_intent="company_snapshot",
            candidate_skill="company_snapshot",
            confidence=0.91,
            reason_codes=["business_profile:company_research.company_snapshot"],
            task_brief=_equity_task_brief("贵州茅台"),
        )
    )
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=llm_client,
    )

    result = router.route(
        message="分析一下贵州茅台",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert result.decision.intent == "task_workflow"
    assert result.decision.domain == "a_share_equity_research"
    assert result.decision.sub_intent == "company_snapshot"
    assert result.decision.candidate_skill == "company_snapshot"
    assert result.decision.risk_level == "medium"
    assert result.task_brief.entities[0].mention == "贵州茅台"
    assert len(llm_client.calls) == 1
    payload = json.loads(llm_client.calls[0]["user_prompt"])
    assert payload["matched_profile"]["id"] == (
        "company_research.company_snapshot"
    )


def test_hybrid_intent_router_detects_a_share_financial_report_analysis(
    business_intent_catalog,
):
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=FakeLlmClient(
            _routing_content(
                intent="task_workflow",
                domain="a_share_equity_research",
                sub_intent="financial_report_analysis",
                candidate_skill="financial_report_analysis",
                task_brief=_equity_task_brief("宁德时代"),
            )
        ),
    )

    result = router.route(
        message="分析宁德时代 2024 年报",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert result.decision.intent == "task_workflow"
    assert result.decision.domain == "a_share_equity_research"
    assert result.decision.sub_intent == "financial_report_analysis"
    assert result.decision.candidate_skill == "financial_report_analysis"
    assert result.task_brief is not None


def test_hybrid_intent_router_prefers_financial_report_over_company_snapshot_when_question_mentions_report(
    business_intent_catalog,
):
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=FakeLlmClient(
            _routing_content(
                intent="task_workflow",
                domain="a_share_equity_research",
                sub_intent="financial_report_analysis",
                candidate_skill="financial_report_analysis",
                task_brief=_equity_task_brief("宁德时代"),
            )
        ),
    )

    result = router.route(
        message="分析一下宁德时代 2024 年报",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert result.decision.sub_intent == "financial_report_analysis"
    assert result.decision.candidate_skill == "financial_report_analysis"


def test_hybrid_intent_router_detects_a_share_risk_scan(
    business_intent_catalog,
):
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=FakeLlmClient(
            _routing_content(
                intent="task_workflow",
                domain="a_share_equity_research",
                sub_intent="risk_scan",
                candidate_skill="risk_scan",
                task_brief=_equity_task_brief("隆基绿能"),
            )
        ),
    )

    result = router.route(
        message="看看隆基绿能最近有什么风险",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert result.decision.intent == "task_workflow"
    assert result.decision.domain == "a_share_equity_research"
    assert result.decision.sub_intent == "risk_scan"
    assert result.decision.candidate_skill == "risk_scan"
    assert result.task_brief is not None


def test_hybrid_intent_router_prefers_risk_scan_over_company_snapshot_when_question_mentions_risk(
    business_intent_catalog,
):
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=FakeLlmClient(
            _routing_content(
                intent="task_workflow",
                domain="a_share_equity_research",
                sub_intent="risk_scan",
                candidate_skill="risk_scan",
                task_brief=_equity_task_brief("宁德时代"),
            )
        ),
    )

    result = router.route(
        message="分析一下宁德时代最近有什么风险",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert result.decision.sub_intent == "risk_scan"
    assert result.decision.candidate_skill == "risk_scan"
