import json
from base64 import b64encode
from typing import Any

import httpx
from pydantic import ValidationError

from app.vision.schemas import VisionAnalysis, VisionAnalysisResult
from app.vision.resilience import (
    ProviderCallGuard,
    ProviderCircuitOpenError,
    ProviderConcurrencyLimitError,
)


SYSTEM_PROMPT = """你是 A 股投研图片分析器。请识别财务报表、公告、研报截图、表格和图表，
提取图片中明确可见的信息，不猜测被遮挡或不可辨认的内容。金额、单位、期间、同比/环比必须保持原意。
输出单个 JSON 对象，字段必须符合约定；风险提示应描述图片信息的缺失、模糊或口径限制，不提供买卖建议。"""

USER_PROMPT = """分析这张图片并返回以下 JSON 字段：schema_version 固定为 1.0；
document_type；summary；extracted_text；financial_metrics（name/value/unit/period/source_text）；
chart_observations；entities；risk_flags；confidence。所有自然语言字段使用中文。"""


class VisionProviderError(RuntimeError):
    def __init__(self, *, code: str, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__("图片分析服务调用失败。")


class OpenAICompatibleVisionClient:
    provider = "openai_compatible"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 2048,
        http_client: httpx.Client | None = None,
        call_guard: ProviderCallGuard | None = None,
    ) -> None:
        if not api_key.strip() or not model.strip() or not base_url.strip():
            raise ValueError("Vision provider 配置不完整。")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._http_client = http_client
        self._call_guard = call_guard

    @property
    def model(self) -> str:
        return self._model

    def analyze(self, image: bytes, *, media_type: str) -> VisionAnalysisResult:
        if self._call_guard is None:
            return self._analyze(image=image, media_type=media_type)
        try:
            return self._call_guard.call(
                lambda: self._analyze(image=image, media_type=media_type)
            )
        except (ProviderCircuitOpenError, ProviderConcurrencyLimitError) as exc:
            raise VisionProviderError(code=exc.code) from exc

    def _analyze(self, *, image: bytes, media_type: str) -> VisionAnalysisResult:
        payload = self._payload(image=image, media_type=media_type)
        if self._http_client is not None:
            return self._analyze_with_client(self._http_client, payload)
        with httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
        ) as client:
            return self._analyze_with_client(client, payload)

    def _payload(self, *, image: bytes, media_type: str) -> dict[str, Any]:
        encoded = b64encode(image).decode("ascii")
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": USER_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{encoded}"
                            },
                        },
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": self._max_output_tokens,
            "response_format": {"type": "json_object"},
        }

    def _analyze_with_client(
        self,
        client: httpx.Client,
        payload: dict[str, Any],
    ) -> VisionAnalysisResult:
        try:
            response = client.post(
                "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise VisionProviderError(
                code="vision_http_error",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.TimeoutException as exc:
            raise VisionProviderError(code="vision_timeout") from exc
        except httpx.HTTPError as exc:
            raise VisionProviderError(code="vision_unreachable") from exc

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError
            analysis = VisionAnalysis.model_validate(json.loads(content))
            usage = body.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
        except (
            json.JSONDecodeError,
            ValidationError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            raise VisionProviderError(code="vision_invalid_response") from exc
        return VisionAnalysisResult(
            analysis=analysis,
            provider=self.provider,
            model=self._model,
            usage=usage,
        )
