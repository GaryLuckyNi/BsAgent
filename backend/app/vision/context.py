from typing import Protocol

from pydantic import ValidationError

from app.assets.errors import AssetNotReadyError
from app.assets.schemas import AssetAttachment
from app.graph.state import ImageAnalysisSummary
from app.security.principal import Principal
from app.vision.repository import SqlAlchemyVisionAnalysisRepository
from app.vision.schemas import VisionAnalysis


class AttachmentAssetService(Protocol):
    def resolve_attachments(
        self,
        asset_ids: list[str],
        *,
        principal: Principal,
        session_id: str,
    ) -> list[AssetAttachment]: ...


class VisionAttachmentContextResolver:
    def __init__(
        self,
        *,
        asset_service: AttachmentAssetService,
        analysis_repository: SqlAlchemyVisionAnalysisRepository,
        max_ocr_chars: int = 2000,
        max_metrics: int = 30,
        max_chart_observations: int = 30,
    ) -> None:
        self._asset_service = asset_service
        self._analysis_repository = analysis_repository
        self._max_ocr_chars = max_ocr_chars
        self._max_metrics = max_metrics
        self._max_chart_observations = max_chart_observations

    def resolve(
        self,
        asset_ids: list[str],
        *,
        principal: Principal,
        session_id: str,
    ) -> list[ImageAnalysisSummary]:
        assets = self._asset_service.resolve_attachments(
            asset_ids,
            principal=principal,
            session_id=session_id,
        )
        contexts: list[ImageAnalysisSummary] = []
        for asset in assets:
            record = self._analysis_repository.get_latest(
                asset_id=asset.asset_id,
                tenant_id=principal.tenant_id,
            )
            if record is None:
                raise AssetNotReadyError("图片资产尚未生成分析结果。")
            try:
                analysis = VisionAnalysis.model_validate(record.result_json)
            except ValidationError as exc:
                raise AssetNotReadyError("图片资产分析结果不可用。") from exc
            metrics = analysis.financial_metrics[: self._max_metrics]
            observations = analysis.chart_observations[
                : self._max_chart_observations
            ]
            truncated = (
                len(analysis.extracted_text) > self._max_ocr_chars
                or len(metrics) < len(analysis.financial_metrics)
                or len(observations) < len(analysis.chart_observations)
            )
            contexts.append(
                {
                    "asset_id": asset.asset_id,
                    "analysis_id": record.analysis_id,
                    "media_type": asset.media_type,
                    "width": asset.width,
                    "height": asset.height,
                    "status": asset.status.value,
                    "document_type": analysis.document_type,
                    "summary": analysis.summary,
                    "extracted_text": analysis.extracted_text[
                        : self._max_ocr_chars
                    ],
                    "financial_metrics": [
                        metric.model_dump(mode="json") for metric in metrics
                    ],
                    "chart_observations": observations,
                    "entities": analysis.entities,
                    "risk_flags": analysis.risk_flags,
                    "confidence": analysis.confidence,
                    "truncated": truncated,
                }
            )
        return contexts
