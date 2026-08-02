from ocr_service.engine import PaddleOcrEngine


class Result:
    json = {
        "res": {
            "rec_texts": ["营业收入", "100亿元"],
            "rec_scores": [0.9, 0.8],
        }
    }


class Pipeline:
    def predict(self, _pixels):
        return [Result()]


def test_paddle_engine_normalizes_v3_result_payload(monkeypatch):
    engine = PaddleOcrEngine.__new__(PaddleOcrEngine)
    engine._pipeline = Pipeline()
    monkeypatch.setattr(
        "ocr_service.engine.Image.open",
        lambda _: FakeImage(),
    )

    result = engine.extract(b"image", languages=("zh", "en"))

    assert result == {
        "text": "营业收入\n100亿元",
        "confidence": 0.85,
        "warnings": [],
    }


class FakeImage:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def convert(self, _mode):
        return [[[]]]
