import json

import pytest
from app.graph.nodes import intent_router as intent_router_module
from app.graph.nodes.intent_router import build_intent_router_node
from app.graph.state import create_initial_state
from app.intent.router import HybridIntentRouter, IntentClassificationError
from app.intent.schemas import IntentDecisionModel
from app.llm.schemas import LlmCompletionResult, LlmStreamDelta
from app.schemas.messages import Message


class FakeLlmClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(
            content=json.dumps(self.payload, ensure_ascii=False),
            model="fake-intent-model",
            provider="fake",
        )


def _decision(
    *,
    intent_type: str | None = "financial_research",
    domain: str | None = "a_share_equity_research",
    subdomain: str | None = "company_snapshot",
    goal: str | None = "分析海能达当前的投资关注价值",
    confidence: float = 0.91,
) -> dict:
    return {
        "intent_type": intent_type,
        "domain": domain,
        "subdomain": subdomain,
        "goal": goal,
        "confidence": confidence,
    }


def test_business_intent_catalog_has_no_local_file_or_keyword_router(
    business_intent_catalog,
):
    removed_names = ("load_default", "load_from_path", "match_by_keywords")
    assert all(not hasattr(type(business_intent_catalog), name) for name in removed_names)


def test_hybrid_intent_router_requires_explicit_catalog_and_llm(
    business_intent_catalog,
):
    with pytest.raises(TypeError, match="catalog"):
        HybridIntentRouter()
    with pytest.raises(TypeError, match="catalog"):
        HybridIntentRouter(catalog=None, llm_client=FakeLlmClient(_decision()))
    with pytest.raises(IntentClassificationError) as exc_info:
        HybridIntentRouter(
            catalog=business_intent_catalog,
            llm_client=None,
        ).route(
            message="分析海能达",
            tenant_id="tenant-a",
            user_id="user-a",
        )
    assert exc_info.value.code == "intent_classifier_not_configured"


def test_intent_router_builder_has_one_explicit_entrypoint(
    business_intent_catalog,
):
    with pytest.raises(TypeError, match="catalog"):
        build_intent_router_node()
    with pytest.raises(TypeError, match="llm_client"):
        build_intent_router_node(catalog=business_intent_catalog)
    assert not hasattr(intent_router_module, "intent_router_node")


def test_llm_intent_router_returns_only_taxonomy_and_goal(
    business_intent_catalog,
):
    llm = FakeLlmClient(_decision())
    result = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=llm,
    ).route(
        message="海能达现在是否值得关注",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert result == IntentDecisionModel.model_validate(_decision())
    assert set(result.model_dump()) == {
        "intent_type",
        "domain",
        "subdomain",
        "goal",
        "confidence",
    }
    payload = json.loads(llm.calls[0]["user_prompt"])
    assert payload["message"] == "海能达现在是否值得关注"
    assert llm.calls[0]["thinking"] is False
    assert llm.calls[0]["max_tokens"] == 512
    assert "available_skills" not in payload
    assert "available_tools" not in payload
    assert "task_brief" not in payload
    assert "risk_level" not in llm.calls[0]["system_prompt"]


def test_intent_router_passes_compaction_separately_from_recent_history(
    business_intent_catalog,
):
    llm = FakeLlmClient(_decision())
    summary_payload = {
        "goal": "分析海能达",
        "constraints": [],
        "decisions": [],
        "open_items": ["继续"],
        "facts": [],
        "evidence_refs": [],
        "user_preferences": [],
        "superseded": [],
    }
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        request_id="current-request",
        user_message="继续",
        history=[
            Message(
                message_id="cmp-1",
                role="assistant",
                content=json.dumps(summary_payload, ensure_ascii=False),
                metadata={"context_compaction": True},
            ),
            Message(message_id="prior-user", role="user", content="上一问"),
            Message(message_id="prior-ai", role="assistant", content="上一答"),
        ],
    )

    build_intent_router_node(catalog=business_intent_catalog, llm_client=llm)(state)

    payload = json.loads(llm.calls[0]["user_prompt"])
    assert payload["context"]["context_summary"] == summary_payload
    assert [item["content"] for item in payload["context"]["conversation_history"]] == [
        "上一问",
        "上一答",
    ]
    assert "继续" not in [
        item["content"] for item in payload["context"]["conversation_history"]
    ]


def test_out_of_scope_is_represented_by_null_taxonomy(
    business_intent_catalog,
):
    result = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=FakeLlmClient(
            _decision(
                intent_type=None,
                domain=None,
                subdomain=None,
                goal=None,
                confidence=0.98,
            )
        ),
    ).route(
        message="帮我写一个贪吃蛇游戏",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert result.intent_type is None
    assert result.domain is None
    assert result.subdomain is None
    assert result.goal is None


@pytest.mark.parametrize(
    "payload",
    [
        _decision(intent_type="software_development"),
        _decision(domain="unknown_domain"),
        _decision(subdomain="unknown_subdomain"),
    ],
)
def test_router_rejects_non_null_values_outside_admin_catalog(
    business_intent_catalog,
    payload,
):
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=FakeLlmClient(payload),
    )

    with pytest.raises(IntentClassificationError) as exc_info:
        router.route(
            message="分析海能达",
            tenant_id="tenant-a",
            user_id="user-a",
        )

    assert exc_info.value.code == "intent_catalog_value_invalid"
    assert exc_info.value.http_status == 502


def test_router_rejects_partial_out_of_scope_payload(
    business_intent_catalog,
):
    router = HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=FakeLlmClient(_decision(intent_type=None, domain="a_share_equity_research")),
    )

    with pytest.raises(IntentClassificationError) as exc_info:
        router.route(
            message="你好",
            tenant_id="tenant-a",
            user_id="user-a",
        )

    assert exc_info.value.code == "intent_classifier_failed"


def test_intent_router_node_passes_safe_image_summary_and_sets_state(
    business_intent_catalog,
):
    llm = FakeLlmClient(
        _decision(
            goal="分析用户上传图片中的财务信息",
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

    result = build_intent_router_node(
        catalog=business_intent_catalog,
        llm_client=llm,
    )(state)

    payload = json.loads(llm.calls[0]["user_prompt"])
    assert payload["context"]["image_analyses"][0]["summary"] == "营业收入同比增长。"
    assert "OCR 原文" not in str(payload["context"])
    assert result["intent"]["goal"] == "分析用户上传图片中的财务信息"
    assert "task_brief" not in result


def test_intent_router_node_does_not_stream_private_classifier_reasoning(
    business_intent_catalog,
):
    class NativeStreamingLlm(FakeLlmClient):
        def __init__(self) -> None:
            super().__init__(_decision())
            self.used_native_stream = False

        async def complete_streamed(self, *, on_delta, **kwargs):
            self.used_native_stream = True
            return await self.complete(**kwargs)

    llm = NativeStreamingLlm()
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="为什么今天 A 股跌得这么多？",
    )

    build_intent_router_node(
        catalog=business_intent_catalog,
        llm_client=llm,
    )(state)

    assert llm.used_native_stream is False
    assert llm.calls[0]["max_tokens"] == 512
    assert llm.calls[0]["thinking"] is False


def test_classifier_prompt_contains_admin_taxonomy_and_semantic_guard(
    business_intent_catalog,
):
    llm = FakeLlmClient(_decision())
    HybridIntentRouter(
        catalog=business_intent_catalog,
        llm_client=llm,
    ).route(
        message="分析一下海能达",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    request = llm.calls[0]
    payload = json.loads(request["user_prompt"])
    assert payload["allowed_taxonomy"][0]["intent_type"] == "financial_research"
    assert "不要改变用户问题的原意" in request["system_prompt"]
    assert "不要丢失公司名称" in request["system_prompt"]
