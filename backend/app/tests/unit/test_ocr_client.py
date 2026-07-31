import json

import httpx
import pytest

from app.vision.ocr_client import PaddleOcrHttpClient, OcrProviderError


def test_paddle_ocr_client_sends_multipart_and_normalizes_response():
    captured = {}

    def handler(request: httpx.Request):
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "text": "营业收入 100 亿元",
                "tables": [{"rows": [["营业收入", "100亿元"]]}],
                "confidence": 0.93,
                "warnings": [],
            },
        )

    client = PaddleOcrHttpClient(
        base_url="http://ocr.example.test",
        provider_version="paddleocr-v3",
        http_client=httpx.Client(
            base_url="http://ocr.example.test",
            transport=httpx.MockTransport(handler),
        ),
    )

    result = client.extract(b"normalized-image", media_type="image/png")

    request = captured["request"]
    assert request.url.path == "/v1/ocr"
    assert "multipart/form-data" in request.headers["content-type"]
    assert b"normalized-image" in request.content
    assert result.text == "营业收入 100 亿元"
    assert result.tables[0]["rows"][0][0] == "营业收入"
    assert result.provider_version == "paddleocr-v3"


def test_paddle_ocr_client_rejects_invalid_contract_without_leaking_body():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=json.dumps({"secret": "raw-secret"}))
    )
    client = PaddleOcrHttpClient(
        base_url="http://ocr.example.test",
        provider_version="paddleocr-v3",
        http_client=httpx.Client(
            base_url="http://ocr.example.test",
            transport=transport,
        ),
    )

    with pytest.raises(OcrProviderError) as exc_info:
        client.extract(b"normalized-image", media_type="image/png")

    assert exc_info.value.code == "ocr_invalid_response"
    assert "raw-secret" not in str(exc_info.value)


def test_ocr_client_executes_provider_call_through_resilience_guard():
    class RecordingGuard:
        def __init__(self):
            self.calls = 0

        def call(self, operation):
            self.calls += 1
            return operation()

    guard = RecordingGuard()
    client = PaddleOcrHttpClient(
        base_url="http://ocr.example.test",
        provider_version="paddleocr-v3",
        call_guard=guard,
        http_client=httpx.Client(
            base_url="http://ocr.example.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"text": "正文", "confidence": 0.9},
                )
            ),
        ),
    )

    client.extract(b"normalized-image", media_type="image/png")

    assert guard.calls == 1
