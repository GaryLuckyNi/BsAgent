from typing import Any

import httpx
from pydantic import ValidationError

from app.vision.schemas import OcrExtractionResult
from app.vision.resilience import (
    ProviderCallGuard,
    ProviderCircuitOpenError,
    ProviderConcurrencyLimitError,
)


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
            return self._call_guard.call(
                lambda: self._extract(image=image, media_type=media_type)
            )
        except (ProviderCircuitOpenError, ProviderConcurrencyLimitError) as exc:
            raise OcrProviderError(code=exc.code) from exc

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
        headers = (
            {"Authorization": f"Bearer {self._api_key}"}
            if self._api_key
            else None
        )
        try:
            response = client.post(
                "/v1/ocr",
                files={"file": (f"image.{extension}", image, media_type)},
                data={"languages": "zh,en", "detect_tables": "true"},
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OcrProviderError(
                code="ocr_http_error",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.TimeoutException as exc:
            raise OcrProviderError(code="ocr_timeout") from exc
        except httpx.HTTPError as exc:
            raise OcrProviderError(code="ocr_unreachable") from exc

        try:
            payload: dict[str, Any] = response.json()
            payload["provider"] = self.provider
            payload["provider_version"] = self._provider_version
            return OcrExtractionResult.model_validate(payload)
        except (ValidationError, ValueError, TypeError) as exc:
            raise OcrProviderError(code="ocr_invalid_response") from exc
