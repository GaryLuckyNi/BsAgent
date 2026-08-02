from hashlib import sha256
from io import BytesIO
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.assets.service import (
    AssetService,
    NoopImageAnalysisDispatcher,
    SqlAlchemyAssetUnitOfWork,
)
from app.assets.schemas import AssetStatus
from app.security.principal import Principal
from app.storage.models import AssetAnalysisRecord, AssetRecord, Base
from app.vision.coordinator import AnalysisCacheIdentity, ImageAnalysisUnavailableError
from app.vision.processor import (
    VisionAnalysisProcessor,
    VisionProcessingError,
    VisionTaskRequest,
)
from app.vision.schemas import CoordinatedImageAnalysisResult, VisionAnalysis


class MemoryObjectStore:
    def __init__(self):
        self.objects = {}

    def put(self, key, data, *, media_type):
        del media_type
        self.objects[key] = data

    def get(self, key):
        return self.objects[key]

    def delete(self, key):
        self.objects.pop(key, None)


class RecordingAnalysisCoordinator:
    def __init__(self, *, fail=False, status="ready", confidence=0.9):
        self.fail = fail
        self.status = status
        self.confidence = confidence
        self.calls = []

    def cache_identity(self, *, tenant_id):
        del tenant_id
        return AnalysisCacheIdentity(
            profile="financial_image",
            ocr_provider="paddleocr_http",
            ocr_version="paddleocr-v3",
            vision_provider="openai_compatible",
            vision_version="vision-model",
            vision_required=True,
        )

    def analyze(self, image, *, media_type, tenant_id):
        self.calls.append((image, media_type, tenant_id))
        if self.fail:
            raise ImageAnalysisUnavailableError(
                codes=("ocr_timeout", "vision_timeout"),
                retryable=True,
            )
        return CoordinatedImageAnalysisResult(
            status=self.status,
            analysis=VisionAnalysis(
                document_type="chart",
                summary="营业收入同比增长。",
                extracted_text="营业收入 100 亿元",
                confidence=self.confidence,
            ),
            provider="ocr_vision_hybrid",
            model="vision-model",
            usage={"total_tokens": 100},
            ocr_status="success",
            vision_status="success" if self.status == "ready" else "failed",
            ocr_provider="paddleocr_http",
            ocr_version="paddleocr-v3",
            vision_provider="openai_compatible",
            vision_version="vision-model",
            full_ocr_text="营业收入 100 亿元",
            ocr_tables=[],
        )


def _sanitizer(*_, **__):
    data = b"normalized-image"
    from app.assets.image_security import SanitizedImage

    return SanitizedImage(
        data=data,
        media_type="image/png",
        width=32,
        height=16,
        sha256=sha256(data).hexdigest(),
    )


def _seed_asset(
    session,
    store,
    *,
    asset_id="asset-1",
    analysis_id="analysis-1",
):
    service = AssetService(
        uow=SqlAlchemyAssetUnitOfWork(session),
        object_store=store,
        dispatcher=NoopImageAnalysisDispatcher(),
        sanitizer=_sanitizer,
        max_image_bytes=1024,
        max_image_pixels=1000,
        retention_days=7,
        asset_id_factory=lambda: asset_id,
        analysis_id_factory=lambda: analysis_id,
    )
    return service.upload_image(
        BytesIO(b"source"),
        claimed_media_type="image/png",
        principal=Principal(
            tenant_id="tenant-1",
            user_id="user-1",
            scopes=("asset:write",),
        ),
    )


def _request(record):
    return VisionTaskRequest(
        asset_id=record.asset_id,
        analysis_id=record.analysis_id,
        tenant_id=record.tenant_id,
        user_id=record.user_id,
        session_id=record.session_id,
    )


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def test_vision_processor_persists_result_and_marks_asset_ready(session):
    store = MemoryObjectStore()
    record = _seed_asset(session, store)
    client = RecordingAnalysisCoordinator()
    processor = VisionAnalysisProcessor(
        session=session,
        object_store=store,
        analysis_coordinator=client,
    )

    result = processor.process(_request(record))

    assert result.analysis_id == "analysis-1"
    assert session.get(AssetRecord, record.asset_id).status == AssetStatus.READY
    saved = session.get(AssetAnalysisRecord, "analysis-1")
    assert saved.result_json["summary"] == "营业收入同比增长。"
    assert saved.artifact_storage_key in store.objects
    artifact = json.loads(store.objects[saved.artifact_storage_key])
    assert artifact["full_ocr_text"] == "营业收入 100 亿元"
    assert "full_ocr_text" not in saved.result_json
    assert saved.ocr_status == "success"
    assert saved.vision_status == "success"
    assert client.calls == [(b"normalized-image", "image/png", "tenant-1")]

    duplicate = processor.process(_request(record))

    assert duplicate.analysis_id == "analysis-1"
    assert len(client.calls) == 1


def test_vision_processor_marks_low_confidence_result_partial(session):
    store = MemoryObjectStore()
    record = _seed_asset(session, store)
    processor = VisionAnalysisProcessor(
        session=session,
        object_store=store,
        analysis_coordinator=RecordingAnalysisCoordinator(
            status="partial",
            confidence=0.3,
        ),
    )

    processor.process(_request(record))

    assert session.get(AssetRecord, record.asset_id).status == AssetStatus.PARTIAL


def test_vision_processor_persists_safe_failure_and_can_retry(session):
    store = MemoryObjectStore()
    record = _seed_asset(session, store)
    client = RecordingAnalysisCoordinator(fail=True)
    processor = VisionAnalysisProcessor(
        session=session,
        object_store=store,
        analysis_coordinator=client,
    )

    with pytest.raises(VisionProcessingError) as exc_info:
        processor.process(_request(record))

    assert exc_info.value.code == "image_analysis_unavailable"
    failed = session.get(AssetRecord, record.asset_id)
    assert failed.status == AssetStatus.FAILED
    assert failed.failure_code == "image_analysis_unavailable"

    client.fail = False
    result = processor.process(_request(record))

    assert result.analysis_id == "analysis-1"
    assert session.get(AssetRecord, record.asset_id).status == AssetStatus.READY


def test_vision_processor_reuses_same_tenant_analysis_cache_with_new_ids(session):
    store = MemoryObjectStore()
    first = _seed_asset(session, store)
    coordinator = RecordingAnalysisCoordinator()
    processor = VisionAnalysisProcessor(
        session=session,
        object_store=store,
        analysis_coordinator=coordinator,
    )
    processor.process(_request(first))
    second = _seed_asset(
        session,
        store,
        asset_id="asset-2",
        analysis_id="analysis-2",
    )

    result = processor.process(_request(second))

    assert len(coordinator.calls) == 1
    assert result.analysis_id == "analysis-2"
    assert result.asset_id == "asset-2"
    assert result.artifact_storage_key != session.get(
        AssetAnalysisRecord,
        "analysis-1",
    ).artifact_storage_key
    assert result.artifact_storage_key in store.objects
    assert session.get(AssetRecord, "asset-2").status == AssetStatus.READY
