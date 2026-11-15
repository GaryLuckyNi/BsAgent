import json

import httpx
import pytest

from app.vision.client import OpenAICompatibleVisionClient, VisionProviderError


def _analysis_payload():
    return {
        "schema_version": "1.0",
        "document_type": "financial_statement",
        "summary": "图片展示某公司 2025 年营业收入同比增长。",
        "extracted_text": "营业收入 100 亿元，同比增长 12%",
        "financial_metrics": [
            {
                "name": "营业收入",
                "value": "100",
                "unit": "亿元",
                "period": "2025",
                "source_text": "营业收入 100 亿元",
            }
        ],
        "chart_observations": ["营业收入同比增长 12%"],
        "entities": ["营业收入"],
        "risk_flags": ["图片未展示审计意见"],
        "confidence": 0.91,
    }


def test_vision_client_sends_private_image_as_data_url_and_parses_contract():
    captured = {}

    def handler(request: httpx.Request):
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(_analysis_payload())}}
                ],
                "usage": {"total_tokens": 123},
            },
        )

    client = OpenAICompatibleVisionClient(
        api_key="secret-key",
        model="vision-model",
        base_url="https://vision.example.test/v1",
        http_client=httpx.Client(
            base_url="https://vision.example.test/v1",
            transport=httpx.MockTransport(handler),
        ),
    )

    result = client.analyze(b"normalized-image", media_type="image/png")

    request_payload = json.loads(captured["request"].content)
    user_content = request_payload["messages"][1]["content"]
    assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert request_payload["response_format"] == {"type": "json_object"}
    assert result.analysis.document_type == "financial_statement"
    assert result.analysis.financial_metrics[0].name == "营业收入"
    assert result.provider == "openai_compatible"
    assert result.usage == {"total_tokens": 123}


def test_vision_client_rejects_non_contract_provider_response():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not-json"}}]},
        )
    )
    client = OpenAICompatibleVisionClient(
        api_key="secret-key",
        model="vision-model",
        base_url="https://vision.example.test/v1",
        http_client=httpx.Client(
            base_url="https://vision.example.test/v1",
            transport=transport,
        ),
    )

    with pytest.raises(VisionProviderError) as exc_info:
        client.analyze(b"normalized-image", media_type="image/png")

    assert exc_info.value.code == "vision_invalid_response"
    assert "not-json" not in str(exc_info.value)


def test_vision_client_maps_timeout_to_stable_error():
    def timeout(_: httpx.Request):
        raise httpx.ReadTimeout("secret upstream detail")

    client = OpenAICompatibleVisionClient(
        api_key="secret-key",
        model="vision-model",
        base_url="https://vision.example.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(timeout)),
    )

    with pytest.raises(VisionProviderError) as exc_info:
        client.analyze(b"normalized-image", media_type="image/png")

    assert exc_info.value.code == "vision_timeout"
    assert "secret upstream detail" not in str(exc_info.value)


def test_vision_client_executes_provider_call_through_resilience_guard():
    class RecordingGuard:
        def __init__(self):
            self.calls = 0

        def call(self, operation):
            self.calls += 1
            return operation()

    guard = RecordingGuard()
    client = OpenAICompatibleVisionClient(
        api_key="secret-key",
        model="vision-model",
        base_url="https://vision.example.test/v1",
        call_guard=guard,
        http_client=httpx.Client(
            base_url="https://vision.example.test/v1",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(_analysis_payload())
                                }
                            }
                        ]
                    },
                )
            ),
        ),
    )

    client.analyze(b"normalized-image", media_type="image/png")

    assert guard.calls == 1
