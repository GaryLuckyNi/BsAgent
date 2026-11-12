import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.assets.object_store import (
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    validate_object_key,
)
from app.assets.repository import SqlAlchemyAssetRepository
from app.assets.schemas import AssetStatus
from app.security.asset_authorization import (
    AssetAuthorizationDenied,
    AssetAuthorizationUnavailable,
    authorized_image_processing,
    check_active_asset_authorization,
    require_queued_asset_authorization,
)
from app.security.image_content import (
    ImageContentRejected,
    ImageContentUnavailable,
    protect_image_payload,
    protect_image_result,
)
from app.storage.models import AssetAnalysisRecord
from app.vision.coordinator import AnalysisCacheIdentity, ImageAnalysisUnavailableError
from app.vision.repository import SqlAlchemyVisionAnalysisRepository
from app.vision.schemas import CoordinatedImageAnalysisResult


class AnalysisCoordinator(Protocol):
    def cache_identity(self, *, tenant_id: str) -> AnalysisCacheIdentity: ...

    def analyze(
        self,
        image: bytes,
        *,
        media_type: str,
        tenant_id: str,
    ) -> CoordinatedImageAnalysisResult: ...


@dataclass(frozen=True)
class VisionTaskRequest:
    asset_id: str
    analysis_id: str
    tenant_id: str
    user_id: str
    session_id: str | None = None
    trace_id: str | None = None


class VisionProcessingError(RuntimeError):
    def __init__(self, *, code: str, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__("图片分析任务执行失败。")


class VisionAnalysisProcessor:
    def __init__(
        self,
        *,
        session: Session,
        object_store: ObjectStore,
        analysis_coordinator: AnalysisCoordinator,
    ) -> None:
        self._session = session
        self._assets = SqlAlchemyAssetRepository(session)
        self._analyses = SqlAlchemyVisionAnalysisRepository(session)
        self._object_store = object_store
        self._analysis_coordinator = analysis_coordinator

    def process(
        self,
        request: VisionTaskRequest,
    ) -> AssetAnalysisRecord:
        failure = None
        content_failure = False
        try:
            with authorized_image_processing(lambda: self._authorized_asset(request)):
                return self._process(request)
        except (ImageContentRejected, ImageContentUnavailable) as exc:
            failure = (exc.code, exc.retryable)
            content_failure = True
        except (
            AssetAuthorizationDenied,
            AssetAuthorizationUnavailable,
            VisionProcessingError,
        ) as exc:
            failure = (exc.code, exc.retryable)
        if content_failure:
            try:
                asset = self._authorized_asset(request)
                if asset.status == AssetStatus.PROCESSING:
                    self._mark_failed(
                        asset_id=asset.asset_id, tenant_id=asset.tenant_id, code=failure[0]
                    )
            except (
                AssetAuthorizationDenied,
                AssetAuthorizationUnavailable,
                VisionProcessingError,
            ) as exc:
                failure = (exc.code, exc.retryable)
        raise VisionProcessingError(code=failure[0], retryable=failure[1])

    def _authorized_asset(self, request):
        failed = False
        try:
            asset = self._assets.get(request.asset_id, tenant_id=request.tenant_id)
        except SQLAlchemyError:
            failed = True
        if failed:
            raise AssetAuthorizationUnavailable
        if asset is None:
            raise VisionProcessingError(code="asset_not_found", retryable=False)
        require_queued_asset_authorization(asset, request)
        return asset

    def _process(self, request):
        asset = self._authorized_asset(request)

        existing = self._analyses.get_latest(
            asset_id=asset.asset_id,
            tenant_id=asset.tenant_id,
        )
        status = AssetStatus(asset.status)
        if existing is not None and status in {AssetStatus.READY, AssetStatus.PARTIAL}:
            return existing
        if status == AssetStatus.FAILED:
            asset = self._assets.transition_status(
                asset_id=asset.asset_id,
                tenant_id=asset.tenant_id,
                expected_status=AssetStatus.FAILED,
                target_status=AssetStatus.PROCESSING,
            )
            self._session.commit()
            status = AssetStatus.PROCESSING
        if status != AssetStatus.PROCESSING:
            raise VisionProcessingError(code="asset_not_processable", retryable=False)

        cached = self._find_cached(asset)
        if cached is not None:
            reused = self._reuse_cached(
                source=cached,
                asset_id=asset.asset_id,
                tenant_id=asset.tenant_id,
                analysis_id=request.analysis_id,
            )
            if reused is not None:
                return reused

        check_active_asset_authorization()
        try:
            image = self._object_store.get(asset.object_key)
        except ObjectNotFoundError as exc:
            self._mark_failed(
                asset_id=asset.asset_id, tenant_id=asset.tenant_id, code="asset_object_missing"
            )
            raise VisionProcessingError(
                code="asset_object_missing",
                retryable=False,
            ) from exc
        except ObjectStoreError as exc:
            self._mark_failed(
                asset_id=asset.asset_id, tenant_id=asset.tenant_id, code="asset_store_unavailable"
            )
            raise VisionProcessingError(
                code="asset_store_unavailable",
                retryable=True,
            ) from exc

        try:
            check_active_asset_authorization()
            result = self._analysis_coordinator.analyze(
                image,
                media_type=asset.media_type,
                tenant_id=asset.tenant_id,
            )
        except ImageAnalysisUnavailableError as exc:
            self._mark_failed(
                asset_id=asset.asset_id,
                tenant_id=asset.tenant_id,
                code="image_analysis_unavailable",
            )
            raise VisionProcessingError(
                code="image_analysis_unavailable",
                retryable=exc.retryable,
            ) from exc

        check_active_asset_authorization()
        result = protect_image_result(result)
        target_status = AssetStatus(result.status)
        artifact_storage_key = self._artifact_storage_key(
            tenant_id=asset.tenant_id,
            analysis_id=request.analysis_id,
        )
        artifact_written = False
        try:
            self._object_store.put(
                artifact_storage_key,
                json.dumps(
                    {
                        "analysis_id": request.analysis_id,
                        "asset_id": asset.asset_id,
                        "profile": "financial_image",
                        "analysis": result.analysis.model_dump(mode="json"),
                        "full_ocr_text": result.full_ocr_text,
                        "ocr_tables": result.ocr_tables,
                        "ocr_status": result.ocr_status,
                        "vision_status": result.vision_status,
                        "ocr_provider": result.ocr_provider,
                        "ocr_version": result.ocr_version,
                        "vision_provider": result.vision_provider,
                        "vision_version": result.vision_version,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8"),
                media_type="application/json",
            )
            artifact_written = True
            analysis = self._analyses.save(
                analysis_id=request.analysis_id,
                asset_id=asset.asset_id,
                tenant_id=asset.tenant_id,
                result=result,
                artifact_storage_key=artifact_storage_key,
            )
            self._assets.transition_status(
                asset_id=asset.asset_id,
                tenant_id=asset.tenant_id,
                expected_status=AssetStatus.PROCESSING,
                target_status=target_status,
            )
            self._session.commit()
            return analysis
        except Exception as exc:
            self._session.rollback()
            if artifact_written:
                try:
                    self._object_store.delete(artifact_storage_key)
                except ObjectStoreError:
                    pass
            raise VisionProcessingError(
                code="vision_persistence_failed",
                retryable=True,
            ) from exc

    @staticmethod
    def _artifact_storage_key(*, tenant_id: str, analysis_id: str) -> str:
        tenant_namespace = sha256(tenant_id.encode("utf-8")).hexdigest()[:24]
        return validate_object_key(f"tenants/{tenant_namespace}/analysis/{analysis_id}.json")

    def _mark_failed(self, *, asset_id: str, tenant_id: str, code: str) -> None:
        try:
            self._assets.transition_status(
                asset_id=asset_id,
                tenant_id=tenant_id,
                expected_status=AssetStatus.PROCESSING,
                target_status=AssetStatus.FAILED,
                failure_code=code[:64],
            )
            self._session.commit()
        except Exception as exc:
            self._session.rollback()
            raise VisionProcessingError(
                code="vision_failure_state_persistence_failed",
                retryable=True,
            ) from exc

    def _find_cached(self, asset) -> AssetAnalysisRecord | None:
        cache_identity = getattr(self._analysis_coordinator, "cache_identity", None)
        if not callable(cache_identity):
            return None
        identity = cache_identity(tenant_id=asset.tenant_id)
        try:
            return self._analyses.find_cached(
                tenant_id=asset.tenant_id,
                image_sha256=asset.sha256,
                identity=identity,
            )
        except ImageContentRejected:
            return None  # 旧缓存格式坏或超限可重算；检查器故障不作为 cache miss。

    def _reuse_cached(
        self,
        *,
        source: AssetAnalysisRecord,
        asset_id: str,
        tenant_id: str,
        analysis_id: str,
    ) -> AssetAnalysisRecord | None:
        if not source.artifact_storage_key:
            return None
        check_active_asset_authorization()
        try:
            artifact = json.loads(
                self._object_store.get(source.artifact_storage_key).decode("utf-8")
            )
            artifact["analysis_id"] = analysis_id
            artifact["asset_id"] = asset_id
            artifact = protect_image_payload(artifact)
        except (
            ObjectStoreError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            RecursionError,
            ImageContentRejected,
        ):
            return None

        target_key = self._artifact_storage_key(
            tenant_id=tenant_id,
            analysis_id=analysis_id,
        )
        written = False
        check_active_asset_authorization()
        try:
            self._object_store.put(
                target_key,
                json.dumps(
                    artifact,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8"),
                media_type="application/json",
            )
            written = True
            cloned = self._analyses.clone_cached(
                source=source,
                analysis_id=analysis_id,
                asset_id=asset_id,
                artifact_storage_key=target_key,
            )
            target_status = (
                AssetStatus.READY
                if source.ocr_status == "success" and source.vision_status == "success"
                else AssetStatus.PARTIAL
            )
            self._assets.transition_status(
                asset_id=asset_id,
                tenant_id=tenant_id,
                expected_status=AssetStatus.PROCESSING,
                target_status=target_status,
            )
            self._session.commit()
            return cloned
        except Exception as exc:  # 缓存 IO 补偿；强制内容检查故障继续抛出。
            self._session.rollback()
            if written:
                try:
                    self._object_store.delete(target_key)
                except ObjectStoreError:
                    pass
            if isinstance(exc, (ImageContentRejected, ImageContentUnavailable)):
                raise
            return None
