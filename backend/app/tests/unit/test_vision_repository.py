from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.storage.models import Base
from app.vision.repository import SqlAlchemyVisionAnalysisRepository
from app.vision.schemas import VisionAnalysis, VisionAnalysisResult


def _result(*, summary="第一版", model="vision-v1"):
    return VisionAnalysisResult(
        analysis=VisionAnalysis(
            document_type="chart",
            summary=summary,
            extracted_text="营业收入 100 亿元",
            chart_observations=["同比增长"],
            confidence=0.9,
        ),
        provider="openai_compatible",
        model=model,
        usage={"total_tokens": 123},
    )


def test_vision_repository_keeps_versions_and_returns_latest_inside_tenant():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = SqlAlchemyVisionAnalysisRepository(session)
        repository.save(
            analysis_id="analysis-1",
            asset_id="asset-1",
            tenant_id="tenant-1",
            result=_result(summary="第一版", model="vision-v1"),
        )
        repository.save(
            analysis_id="analysis-2",
            asset_id="asset-1",
            tenant_id="tenant-1",
            result=_result(summary="第二版", model="vision-v2"),
        )
        session.commit()

        latest = repository.get_latest(asset_id="asset-1", tenant_id="tenant-1")

        assert latest.analysis_id == "analysis-2"
        assert latest.model == "vision-v2"
        assert latest.result_json["summary"] == "第二版"
        assert latest.usage_json == {"total_tokens": 123}
        assert repository.get_latest(
            asset_id="asset-1",
            tenant_id="tenant-2",
        ) is None
