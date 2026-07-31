from dataclasses import dataclass
import json
import re
from typing import Protocol
import unicodedata

from app.vision.client import VisionProviderError
from app.vision.ocr_client import OcrProviderError
from app.vision.schemas import (
    CoordinatedImageAnalysisResult,
    OcrExtractionResult,
    VisionAnalysis,
    VisionAnalysisResult,
)


class OcrClient(Protocol):
    def extract(self, image: bytes, *, media_type: str) -> OcrExtractionResult: ...


class VisionClient(Protocol):
    def analyze(self, image: bytes, *, media_type: str) -> VisionAnalysisResult: ...


@dataclass(frozen=True)
class TenantImagePolicy:
    allow_external_vision: bool


@dataclass(frozen=True)
class AnalysisCacheIdentity:
    profile: str
    ocr_provider: str
    ocr_version: str
    vision_provider: str
    vision_version: str
    vision_required: bool


class TenantImagePolicyProvider(Protocol):
    def get_policy(self, *, tenant_id: str) -> TenantImagePolicy: ...


class StaticTenantImagePolicyProvider:
    def __init__(self, *, allow_external_vision: bool) -> None:
        self._policy = TenantImagePolicy(
            allow_external_vision=allow_external_vision
        )

    def get_policy(self, *, tenant_id: str) -> TenantImagePolicy:
        del tenant_id
        return self._policy


class ImageAnalysisUnavailableError(RuntimeError):
    def __init__(self, *, codes: tuple[str, ...], retryable: bool) -> None:
        self.codes = codes
        self.retryable = retryable
        super().__init__("OCR 与图片分析服务均不可用。")


class ImageAnalysisCoordinator:
    def __init__(
        self,
        *,
        ocr_client: OcrClient,
        vision_client: VisionClient,
        policy_provider: TenantImagePolicyProvider,
        max_ocr_excerpt_chars: int = 2000,
    ) -> None:
        self._ocr_client = ocr_client
        self._vision_client = vision_client
        self._policy_provider = policy_provider
        self._max_ocr_excerpt_chars = max_ocr_excerpt_chars

    def cache_identity(self, *, tenant_id: str) -> AnalysisCacheIdentity:
        policy = self._policy_provider.get_policy(tenant_id=tenant_id)
        return AnalysisCacheIdentity(
            profile="financial_image",
            ocr_provider=getattr(self._ocr_client, "provider", "paddleocr_http"),
            ocr_version=getattr(self._ocr_client, "provider_version", "unknown"),
            vision_provider=getattr(
                self._vision_client,
                "provider",
                "openai_compatible",
            ),
            vision_version=getattr(self._vision_client, "model", "unknown"),
            vision_required=policy.allow_external_vision,
        )

    def analyze(
        self,
        image: bytes,
        *,
        media_type: str,
        tenant_id: str,
    ) -> CoordinatedImageAnalysisResult:
        ocr_result: OcrExtractionResult | None = None
        vision_result: VisionAnalysisResult | None = None
        failures: list[OcrProviderError | VisionProviderError] = []
        risk_flags: list[str] = []

        try:
            ocr_result = self._ocr_client.extract(image, media_type=media_type)
        except OcrProviderError as exc:
            failures.append(exc)
            risk_flags.append(exc.code)

        policy = self._policy_provider.get_policy(tenant_id=tenant_id)
        if policy.allow_external_vision:
            try:
                vision_result = self._vision_client.analyze(
                    image,
                    media_type=media_type,
                )
            except VisionProviderError as exc:
                failures.append(exc)
                risk_flags.append(exc.code)
            vision_status = "success" if vision_result is not None else "failed"
        else:
            vision_status = "skipped"
            risk_flags.append("external_vision_denied_by_tenant_policy")

        if ocr_result is None and vision_result is None:
            codes = tuple(error.code for error in failures)
            if vision_status == "skipped":
                codes = (*codes, "external_vision_denied_by_tenant_policy")
            raise ImageAnalysisUnavailableError(
                codes=codes,
                retryable=any(_is_retryable(error) for error in failures),
            )

        vision_analysis = vision_result.analysis if vision_result is not None else None
        full_ocr_text = ocr_result.text if ocr_result is not None else ""
        excerpt = full_ocr_text[: self._max_ocr_excerpt_chars]
        ocr_tables = ocr_result.tables if ocr_result is not None else []
        if len(full_ocr_text) > len(excerpt):
            risk_flags.append("ocr_excerpt_truncated")
        if ocr_result is not None:
            risk_flags.extend(ocr_result.warnings)
        if vision_analysis is not None:
            risk_flags.extend(vision_analysis.risk_flags)
        if ocr_result is not None and vision_analysis is not None:
            risk_flags.extend(
                _metric_conflict_warnings(
                    ocr_result=ocr_result,
                    vision_analysis=vision_analysis,
                )
            )

        confidence_values = [
            value
            for value in (
                ocr_result.confidence if ocr_result is not None else None,
                vision_analysis.confidence if vision_analysis is not None else None,
            )
            if value is not None
        ]
        confidence = sum(confidence_values) / len(confidence_values)
        both_succeeded = ocr_result is not None and vision_result is not None
        analysis = VisionAnalysis(
            document_type=(
                vision_analysis.document_type
                if vision_analysis is not None
                else "table"
                if ocr_tables
                else "screenshot"
            ),
            summary=(
                vision_analysis.summary
                if vision_analysis is not None
                else _ocr_summary(excerpt)
            ),
            extracted_text=excerpt,
            financial_metrics=(
                vision_analysis.financial_metrics
                if vision_analysis is not None
                else []
            ),
            chart_observations=(
                vision_analysis.chart_observations
                if vision_analysis is not None
                else []
            ),
            entities=vision_analysis.entities if vision_analysis is not None else [],
            risk_flags=list(dict.fromkeys(risk_flags)),
            confidence=confidence if both_succeeded else confidence * 0.85,
        )
        return CoordinatedImageAnalysisResult(
            status="ready" if both_succeeded else "partial",
            analysis=analysis,
            provider="ocr_vision_hybrid" if both_succeeded else "partial_image_analysis",
            model=vision_result.model if vision_result is not None else "ocr-only",
            usage=vision_result.usage if vision_result is not None else {},
            ocr_status="success" if ocr_result is not None else "failed",
            vision_status=vision_status,
            ocr_provider=(
                ocr_result.provider
                if ocr_result is not None
                else getattr(self._ocr_client, "provider", "paddleocr_http")
            ),
            ocr_version=(
                ocr_result.provider_version
                if ocr_result is not None
                else getattr(self._ocr_client, "provider_version", "unknown")
            ),
            vision_provider=(
                vision_result.provider
                if vision_result is not None
                else getattr(self._vision_client, "provider", "openai_compatible")
            ),
            vision_version=(
                vision_result.model
                if vision_result is not None
                else getattr(self._vision_client, "model", "unknown")
            ),
            full_ocr_text=full_ocr_text,
            ocr_tables=ocr_tables,
        )


def _ocr_summary(excerpt: str) -> str:
    compact = " ".join(excerpt.split())
    if not compact:
        return "OCR 未识别到可用正文，但识别到了结构化表格。"
    return f"OCR 识别内容：{compact[:300]}"


def _is_retryable(error: OcrProviderError | VisionProviderError) -> bool:
    if error.code.endswith(("_timeout", "_unreachable")):
        return True
    if error.code.endswith(("_circuit_open", "_concurrency_limited")):
        return True
    return error.code.endswith("_http_error") and error.status_code in {
        429,
        500,
        502,
        503,
        504,
    }


def _metric_conflict_warnings(
    *,
    ocr_result: OcrExtractionResult,
    vision_analysis: VisionAnalysis,
) -> list[str]:
    ocr_evidence = _normalize_metric_text(
        f"{ocr_result.text} {json.dumps(ocr_result.tables, ensure_ascii=False)}"
    )
    warnings = []
    for metric in vision_analysis.financial_metrics:
        name = _normalize_metric_text(metric.name)
        expected_value = _normalize_metric_text(
            f"{metric.value}{metric.unit or ''}"
        )
        if name and name in ocr_evidence and expected_value not in ocr_evidence:
            warnings.append(f"ocr_vision_metric_conflict:{metric.name}")
    return warnings


def _normalize_metric_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[^0-9a-z.%+\-\u4e00-\u9fff]", "", normalized)
