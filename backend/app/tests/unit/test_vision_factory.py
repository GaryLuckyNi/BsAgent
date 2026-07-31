from app.core.config import Settings
from app.vision import factory


def _vision_settings():
    return Settings(
        _env_file=None,
        vision_provider="openai_compatible",
        vision_api_key="vision-key",
        vision_base_url="https://vision.example.test/v1",
        vision_model="vision-model",
        ocr_provider="paddleocr_http",
        ocr_base_url="http://ocr.example.test",
        ocr_provider_version="paddleocr-v3",
    )


def test_provider_factories_inject_shared_resilience_guards(monkeypatch):
    guards = {"vision": object(), "ocr": object()}
    calls = []

    def build_guard(*, provider_name, config):
        calls.append((provider_name, config))
        return guards[provider_name]

    monkeypatch.setattr(factory, "build_provider_call_guard", build_guard)
    config = _vision_settings()

    vision = factory.build_vision_client(config=config)
    ocr = factory.build_ocr_client(config=config)

    assert vision._call_guard is guards["vision"]
    assert ocr._call_guard is guards["ocr"]
    assert calls == [("vision", config), ("ocr", config)]
