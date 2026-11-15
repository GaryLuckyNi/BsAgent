from typing import Any

import httpx
from pydantic import ValidationError

from app.security.asset_authorization import check_active_asset_authorization
from app.security.http_response import HttpResponseBudgetError, bounded_request
from app.security.image_content import protect_image_result
from app.vision.resilience import (
    ProviderCallGuard,
    ProviderCircuitOpenError,
    ProviderConcurrencyLimitError,
)
from app.vision.schemas import OcrExtractionResult


class OcrProviderError(RuntimeError):
    def __init__(self, *, code: str, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__("OCR 服务调用失败。")


class PaddleOcrHttpClient:
    provider = "paddleocr_http"

    def __init__(
        self,
        *,
        base_url: str,
        provider_version: str,
        api_key: str = "",
        timeout_seconds: float = 60.0,
        http_client: httpx.Client | None = None,
        call_guard: ProviderCallGuard | None = None,
    ) -> None:
        if not base_url.strip() or not provider_version.strip():
            raise ValueError("PaddleOCR provider 配置不完整。")
        self._base_url = base_url.rstrip("/")
        self._provider_version = provider_version
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client
        self._call_guard = call_guard

    @property
    def provider_version(self) -> str:
        return self._provider_version

    def extract(self, image: bytes, *, media_type: str) -> OcrExtractionResult:
        if self._call_guard is None:
            return self._extract(image=image, media_type=media_type)
        try:
            return self._call_guard.call(lambda: self._extract(image=image, media_type=media_type))
        except (ProviderCircuitOpenError, ProviderConcurrencyLimitError) as exc:
            failure = OcrProviderError(code=exc.code)
        raise failure

    def _extract(self, *, image: bytes, media_type: str) -> OcrExtractionResult:
        if self._http_client is not None:
            return self._extract_with_client(
                self._http_client,
                image=image,
                media_type=media_type,
            )
        with httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
        ) as client:
            return self._extract_with_client(
                client,
                image=image,
                media_type=media_type,
            )

    def _extract_with_client(
        self,
        client: httpx.Client,
        *,
        image: bytes,
        media_type: str,
    ) -> OcrExtractionResult:
        extension = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
        }.get(media_type, "bin")
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
        check_active_asset_authorization()
        failure = None
        try:
            response = bounded_request(
                client, "POST", "/v1/ocr", timeout_seconds=self._timeout_seconds,
                files={"file": (f"image.{extension}", image, media_type)},
                data={"languages": "zh,en", "detect_tables": "true"},
                headers=headers,
            )
            response.raise_for_status()
        except HttpResponseBudgetError as error:
            failure = OcrProviderError(code="ocr_timeout" if error.code == "http_response_deadline" else "ocr_response_limit_exceeded")
        except httpx.HTTPStatusError as exc:
            failure = OcrProviderError(code="ocr_http_error", status_code=exc.response.status_code)
        except httpx.TimeoutException:
            failure = OcrProviderError(code="ocr_timeout")
        except httpx.HTTPError:
            failure = OcrProviderError(code="ocr_unreachable")
        if failure is not None:
            raise failure

        try:
            payload: dict[str, Any] = response.json()
            payload["provider"] = self.provider
            payload["provider_version"] = self._provider_version
            result = OcrExtractionResult.model_validate(payload)
        except (ValidationError, ValueError, TypeError, RecursionError):
            failure = OcrProviderError(code="ocr_invalid_response")
        if failure is not None:
            raise failure
        return protect_image_result(result)
