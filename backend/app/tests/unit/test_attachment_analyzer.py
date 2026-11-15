import pytest

from app.assets.errors import AssetNotReadyError
from app.graph.nodes.attachment_analyzer import build_attachment_analyzer_node
from app.graph.state import create_initial_state


def _analysis_context():
    return {
        "asset_id": "asset-1",
        "analysis_id": "analysis-1",
        "media_type": "image/png",
        "width": 32,
        "height": 16,
        "status": "ready",
        "document_type": "financial_statement",
        "summary": "营业收入同比增长。",
        "extracted_text": "营业收入 100 亿元",
        "financial_metrics": [],
        "chart_observations": [],
        "entities": ["示例公司"],
        "risk_flags": [],
        "confidence": 0.9,
        "truncated": False,
    }


class RecordingResolver:
    def __init__(self):
        self.calls = []

    def resolve(self, asset_ids, *, principal, session_id):
        self.calls.append((asset_ids, principal, session_id))
        return [_analysis_context()]


def test_attachment_analyzer_resolves_refs_with_state_principal():
    resolver = RecordingResolver()
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析图片",
        permission_scopes=["agent:invoke", "asset:read"],
        attachment_ids=["asset-1"],
    )

    result = build_attachment_analyzer_node(resolver=resolver)(state)

    asset_ids, principal, session_id = resolver.calls[0]
    assert asset_ids == ["asset-1"]
    assert principal.tenant_id == "tenant-1"
    assert principal.user_id == "user-1"
    assert principal.scopes == ("agent:invoke", "asset:read")
    assert session_id == "session-1"
    assert result["attachment_refs"] == [
        {
            "asset_id": "asset-1",
            "analysis_id": "analysis-1",
            "status": "ready",
        }
    ]
    assert result["image_analyses"] == [_analysis_context()]


def test_attachment_analyzer_skips_resolver_without_refs():
    resolver = RecordingResolver()
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="纯文本问题",
    )

    result = build_attachment_analyzer_node(resolver=resolver)(state)

    assert resolver.calls == []
    assert result["attachment_refs"] == []
    assert result["image_analyses"] == []


def test_attachment_analyzer_preserves_safe_asset_error_contract():
    class FailingResolver:
        def resolve(self, *_args, **_kwargs):
            raise AssetNotReadyError("内部状态不可对外泄露")

    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析图片",
        attachment_ids=["asset-1"],
    )

    with pytest.raises(AssetNotReadyError) as exc_info:
        build_attachment_analyzer_node(resolver=FailingResolver())(state)

    assert exc_info.value.code == "asset_not_ready"


def test_attachment_analyzer_enforces_checkpoint_ocr_budget_defensively():
    class OversizedResolver:
        def resolve(self, *_args, **_kwargs):
            context = _analysis_context()
            context["extracted_text"] = "长" * 5000
            return [context]

    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析图片",
        attachment_ids=["asset-1"],
    )

    result = build_attachment_analyzer_node(resolver=OversizedResolver())(state)

    assert len(result["image_analyses"][0]["extracted_text"]) == 2000
    assert result["image_analyses"][0]["truncated"] is True
