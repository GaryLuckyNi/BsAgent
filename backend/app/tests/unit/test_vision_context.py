from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.assets.errors import AssetNotReadyError
from app.assets.schemas import AssetAttachment, AssetStatus
from app.security.principal import Principal
from app.storage.models import Base
from app.vision.context import VisionAttachmentContextResolver
from app.vision.repository import SqlAlchemyVisionAnalysisRepository
from app.vision.schemas import VisionAnalysis, VisionAnalysisResult


class ReadyAssetService:
    def resolve_attachments(self, asset_ids, *, principal, session_id):
        del principal, session_id
        return [
            AssetAttachment(
                asset_id=asset_id,
                media_type="image/png",
                width=32,
                height=16,
                status=AssetStatus.READY,
            )
            for asset_id in asset_ids
        ]


def _principal():
    return Principal(tenant_id="tenant-1", user_id="user-1", scopes=("agent:invoke",))


def test_vision_context_resolver_loads_latest_result_and_applies_budget():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        SqlAlchemyVisionAnalysisRepository(session).save(
            analysis_id="analysis-1",
            asset_id="asset-1",
            tenant_id="tenant-1",
            result=VisionAnalysisResult(
                analysis=VisionAnalysis(
                    document_type="financial_statement",
                    summary="财务报表摘要",
                    extracted_text="一二三四五六七八九十",
                    financial_metrics=[
                        {"name": str(index), "value": str(index)}
                        for index in range(5)
                    ],
                    chart_observations=["观察1", "观察2", "观察3"],
                    risk_flags=["口径待核验"],
                    confidence=0.88,
                ),
                provider="openai_compatible",
                model="vision-model",
            ),
        )
        session.commit()
        resolver = VisionAttachmentContextResolver(
            asset_service=ReadyAssetService(),
            analysis_repository=SqlAlchemyVisionAnalysisRepository(session),
            max_ocr_chars=6,
            max_metrics=2,
            max_chart_observations=2,
        )

        contexts = resolver.resolve(
            ["asset-1"],
            principal=_principal(),
            session_id="session-1",
        )

        assert contexts[0]["summary"] == "财务报表摘要"
        assert contexts[0]["extracted_text"] == "一二三四五六"
        assert len(contexts[0]["financial_metrics"]) == 2
        assert len(contexts[0]["chart_observations"]) == 2
        assert contexts[0]["truncated"] is True


def test_vision_context_resolver_rejects_asset_without_analysis():
    repository = SimpleNamespace(get_latest=lambda **_: None)
    resolver = VisionAttachmentContextResolver(
        asset_service=ReadyAssetService(),
        analysis_repository=repository,
    )

    with pytest.raises(AssetNotReadyError):
        resolver.resolve(
            ["asset-1"],
            principal=_principal(),
            session_id="session-1",
        )
