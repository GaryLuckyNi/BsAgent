from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from ocr_service.main import create_app


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 16), "white").save(output, format="PNG")
    return output.getvalue()


class FakeEngine:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, image: bytes, *, languages: tuple[str, ...]):
        assert image.startswith(b"\x89PNG")
        assert languages == ("zh", "en")
        self.calls += 1
        return {
            "text": "营业收入 100 亿元",
            "confidence": 0.93,
            "warnings": [],
        }


def test_model_is_loaded_once_and_reused_across_requests():
    engine = FakeEngine()
    factory_calls = []
    app = create_app(engine_factory=lambda: factory_calls.append("load") or engine)

    with TestClient(app) as client:
        assert client.get("/health/ready").json() == {
            "status": "ready",
            "model": "PP-OCRv5_mobile",
        }
        first = client.post(
            "/v1/ocr",
            files={"file": ("report.png", _png_bytes(), "image/png")},
            data={"languages": "zh,en", "detect_tables": "true"},
        )
        second = client.post(
            "/v1/ocr",
            files={"file": ("report.png", _png_bytes(), "image/png")},
            data={"languages": "zh,en", "detect_tables": "false"},
        )

    assert factory_calls == ["load"]
    assert engine.calls == 2
    assert first.status_code == 200
    assert first.json()["text"] == "营业收入 100 亿元"
    assert first.json()["tables"] == []
    assert "table_detection_not_enabled" in first.json()["warnings"]
    assert second.json()["warnings"] == []


def test_ocr_rejects_unsupported_media_type_before_inference():
    engine = FakeEngine()
    app = create_app(engine_factory=lambda: engine)

    with TestClient(app) as client:
        response = client.post(
            "/v1/ocr",
            files={"file": ("note.txt", b"not-an-image", "text/plain")},
        )

    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_image_type"
    assert engine.calls == 0


def test_ocr_requires_bearer_token_when_api_key_is_configured():
    app = create_app(engine_factory=FakeEngine, api_key="ocr-secret")

    with TestClient(app) as client:
        unauthorized = client.post(
            "/v1/ocr",
            files={"file": ("report.png", _png_bytes(), "image/png")},
        )
        authorized = client.post(
            "/v1/ocr",
            files={"file": ("report.png", _png_bytes(), "image/png")},
            headers={"Authorization": "Bearer ocr-secret"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
