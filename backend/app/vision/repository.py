from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.storage.models import AssetAnalysisRecord, AssetRecord
from app.vision.coordinator import AnalysisCacheIdentity
from app.vision.schemas import CoordinatedImageAnalysisResult, VisionAnalysisResult


class SqlAlchemyVisionAnalysisRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(
        self,
        *,
        analysis_id: str,
        asset_id: str,
        tenant_id: str,
        result: VisionAnalysisResult | CoordinatedImageAnalysisResult,
        artifact_storage_key: str | None = None,
    ) -> AssetAnalysisRecord:
        analysis = result.analysis
        coordinated = (
            result if isinstance(result, CoordinatedImageAnalysisResult) else None
        )
        record = AssetAnalysisRecord(
            analysis_id=analysis_id,
            asset_id=asset_id,
            tenant_id=tenant_id,
            schema_version=analysis.schema_version,
            provider=result.provider,
            model=result.model,
            summary=analysis.summary,
            extracted_text=analysis.extracted_text,
            result_json=analysis.model_dump(mode="json"),
            usage_json=result.usage,
            confidence=analysis.confidence,
            profile="financial_image",
            ocr_provider=coordinated.ocr_provider if coordinated else None,
            ocr_version=coordinated.ocr_version if coordinated else None,
            ocr_status=coordinated.ocr_status if coordinated else None,
            vision_provider=(
                coordinated.vision_provider if coordinated else result.provider
            ),
            vision_version=coordinated.vision_version if coordinated else result.model,
            vision_status=coordinated.vision_status if coordinated else "success",
            artifact_storage_key=artifact_storage_key,
            warnings_json=analysis.risk_flags,
            completed_at=datetime.now(UTC),
        )
        self._session.add(record)
        self._session.flush()
        return record

    def get_latest(
        self,
        *,
        asset_id: str,
        tenant_id: str,
    ) -> AssetAnalysisRecord | None:
        return self._session.scalar(
            select(AssetAnalysisRecord)
            .where(
                AssetAnalysisRecord.asset_id == asset_id,
                AssetAnalysisRecord.tenant_id == tenant_id,
            )
            .order_by(
                AssetAnalysisRecord.created_at.desc(),
                AssetAnalysisRecord.analysis_id.desc(),
            )
            .limit(1)
        )

    def find_cached(
        self,
        *,
        tenant_id: str,
        image_sha256: str,
        identity: AnalysisCacheIdentity,
    ) -> AssetAnalysisRecord | None:
        expected_vision_status = "success" if identity.vision_required else "skipped"
        return self._session.scalar(
            select(AssetAnalysisRecord)
            .join(AssetRecord, AssetRecord.asset_id == AssetAnalysisRecord.asset_id)
            .where(
                AssetAnalysisRecord.tenant_id == tenant_id,
                AssetRecord.tenant_id == tenant_id,
                AssetRecord.sha256 == image_sha256,
                AssetRecord.status.in_(("ready", "partial")),
                AssetAnalysisRecord.profile == identity.profile,
                AssetAnalysisRecord.ocr_provider == identity.ocr_provider,
                AssetAnalysisRecord.ocr_version == identity.ocr_version,
                AssetAnalysisRecord.ocr_status == "success",
                AssetAnalysisRecord.vision_provider == identity.vision_provider,
                AssetAnalysisRecord.vision_version == identity.vision_version,
                AssetAnalysisRecord.vision_status == expected_vision_status,
            )
            .order_by(AssetAnalysisRecord.completed_at.desc())
            .limit(1)
        )

    def clone_cached(
        self,
        *,
        source: AssetAnalysisRecord,
        analysis_id: str,
        asset_id: str,
        artifact_storage_key: str,
    ) -> AssetAnalysisRecord:
        record = AssetAnalysisRecord(
            analysis_id=analysis_id,
            asset_id=asset_id,
            tenant_id=source.tenant_id,
            schema_version=source.schema_version,
            provider=source.provider,
            model=source.model,
            summary=source.summary,
            extracted_text=source.extracted_text,
            result_json=source.result_json,
            usage_json={},
            confidence=source.confidence,
            profile=source.profile,
            ocr_provider=source.ocr_provider,
            ocr_version=source.ocr_version,
            ocr_status=source.ocr_status,
            vision_provider=source.vision_provider,
            vision_version=source.vision_version,
            vision_status=source.vision_status,
            artifact_storage_key=artifact_storage_key,
            warnings_json=source.warnings_json,
            completed_at=datetime.now(UTC),
        )
        self._session.add(record)
        self._session.flush()
        return record
