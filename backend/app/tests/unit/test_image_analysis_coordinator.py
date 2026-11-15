import pytest

from app.vision.client import VisionProviderError
from app.vision.coordinator import (
    ImageAnalysisCoordinator,
    ImageAnalysisUnavailableError,
    StaticTenantImagePolicyProvider,
)
from app.vision.ocr_client import OcrProviderError
from app.vision.schemas import (
    OcrExtractionResult,
    VisionAnalysis,
    VisionAnalysisResult,
)


class RecordingOcrClient:
    provider = "paddleocr_http"
    provider_version = "paddleocr-v3"

    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = 0

    def extract(self, image, *, media_type):
        del image, media_type
        self.calls += 1
        if self.fail:
            raise OcrProviderError(code="ocr_timeout")
        return OcrExtractionResult(
            text="营业收入 100 亿元",
            tables=[{"rows": [["营业收入", "100亿元"]]}],
            confidence=0.92,
            warnings=[],
            provider_version="paddleocr-v3",
        )


class RecordingVisionClient:
    provider = "openai_compatible"
    model = "vision-model"

    def __init__(self, *, fail=False, metric_value=None):
        self.fail = fail
        self.metric_value = metric_value
        self.calls = 0

    def analyze(self, image, *, media_type):
        del image, media_type
        self.calls += 1
        if self.fail:
            raise VisionProviderError(code="vision_timeout")
        return VisionAnalysisResult(
            analysis=VisionAnalysis(
                document_type="financial_statement",
                summary="财报显示营业收入增长。",
                extracted_text="视觉模型文字不作为 OCR 事实源",
                financial_metrics=(
                    [
                        {
                            "name": "营业收入",
                            "value": self.metric_value,
                            "unit": "亿元",
                        }
                    ]
                    if self.metric_value is not None
                    else []
                ),
                chart_observations=["营业收入增长"],
                confidence=0.88,
            ),
            provider="openai_compatible",
            model="vision-model",
        )


def test_coordinator_merges_ocr_and_vision_as_ready():
    coordinator = ImageAnalysisCoordinator(
        ocr_client=RecordingOcrClient(),
        vision_client=RecordingVisionClient(),
        policy_provider=StaticTenantImagePolicyProvider(allow_external_vision=True),
    )

    result = coordinator.analyze(
        b"normalized-image",
        media_type="image/png",
        tenant_id="tenant-1",
    )

    assert result.status == "ready"
    assert result.analysis.extracted_text == "营业收入 100 亿元"
    assert result.analysis.summary == "财报显示营业收入增长。"
    assert result.ocr_status == "success"
    assert result.vision_status == "success"
    assert result.ocr_tables[0]["rows"][0][0] == "营业收入"


def test_coordinator_returns_partial_when_vision_fails():
    coordinator = ImageAnalysisCoordinator(
        ocr_client=RecordingOcrClient(),
        vision_client=RecordingVisionClient(fail=True),
        policy_provider=StaticTenantImagePolicyProvider(allow_external_vision=True),
    )

    result = coordinator.analyze(
        b"normalized-image",
        media_type="image/png",
        tenant_id="tenant-1",
    )

    assert result.status == "partial"
    assert result.ocr_status == "success"
    assert result.vision_status == "failed"
    assert "vision_timeout" in result.analysis.risk_flags


def test_coordinator_never_calls_external_vision_when_tenant_policy_denies_it():
    vision = RecordingVisionClient()
    coordinator = ImageAnalysisCoordinator(
        ocr_client=RecordingOcrClient(),
        vision_client=vision,
        policy_provider=StaticTenantImagePolicyProvider(allow_external_vision=False),
    )

    result = coordinator.analyze(
        b"normalized-image",
        media_type="image/png",
        tenant_id="tenant-1",
    )

    assert result.status == "partial"
    assert result.vision_status == "skipped"
    assert result.vision_version == "vision-model"
    assert "external_vision_denied_by_tenant_policy" in result.analysis.risk_flags
    assert vision.calls == 0


def test_coordinator_fails_only_when_both_providers_fail():
    coordinator = ImageAnalysisCoordinator(
        ocr_client=RecordingOcrClient(fail=True),
        vision_client=RecordingVisionClient(fail=True),
        policy_provider=StaticTenantImagePolicyProvider(allow_external_vision=True),
    )

    with pytest.raises(ImageAnalysisUnavailableError) as exc_info:
        coordinator.analyze(
            b"normalized-image",
            media_type="image/png",
            tenant_id="tenant-1",
        )

    assert exc_info.value.codes == ("ocr_timeout", "vision_timeout")
    assert exc_info.value.retryable is True


def test_coordinator_warns_when_ocr_and_vision_metric_values_conflict():
    coordinator = ImageAnalysisCoordinator(
        ocr_client=RecordingOcrClient(),
        vision_client=RecordingVisionClient(metric_value="120"),
        policy_provider=StaticTenantImagePolicyProvider(allow_external_vision=True),
    )

    result = coordinator.analyze(
        b"normalized-image",
        media_type="image/png",
        tenant_id="tenant-1",
    )

    assert "ocr_vision_metric_conflict:营业收入" in result.analysis.risk_flags


def test_coordinator_marks_circuit_and_concurrency_failures_retryable():
    class CircuitOpenOcr:
        def extract(self, *_args, **_kwargs):
            raise OcrProviderError(code="ocr_circuit_open")

    class ConcurrencyLimitedVision:
        def analyze(self, *_args, **_kwargs):
            raise VisionProviderError(code="vision_concurrency_limited")

    coordinator = ImageAnalysisCoordinator(
        ocr_client=CircuitOpenOcr(),
        vision_client=ConcurrencyLimitedVision(),
        policy_provider=StaticTenantImagePolicyProvider(allow_external_vision=True),
    )

    with pytest.raises(ImageAnalysisUnavailableError) as exc_info:
        coordinator.analyze(
            b"normalized-image",
            media_type="image/png",
            tenant_id="tenant-1",
        )

    assert exc_info.value.retryable is True
