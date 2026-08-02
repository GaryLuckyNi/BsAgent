import os
from collections.abc import Iterable
from io import BytesIO
from typing import Any, Protocol

import numpy as np
from PIL import Image


class OcrEngine(Protocol):
    def extract(
        self,
        image: bytes,
        *,
        languages: tuple[str, ...],
    ) -> dict[str, Any]: ...


class PaddleOcrEngine:
    def __init__(
        self,
        *,
        model_dir: str,
        device: str,
        cpu_threads: int,
    ) -> None:
        os.environ.setdefault("PADDLE_PDX_CACHE_HOME", model_dir)
        from paddleocr import PaddleOCR

        self._pipeline = PaddleOCR(
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device=device,
            enable_mkldnn=device == "cpu",
            cpu_threads=cpu_threads,
        )

    def extract(
        self,
        image: bytes,
        *,
        languages: tuple[str, ...],
    ) -> dict[str, Any]:
        del languages
        with Image.open(BytesIO(image)) as source:
            pixels = np.asarray(source.convert("RGB"))
        results = self._pipeline.predict(pixels)
        texts: list[str] = []
        scores: list[float] = []
        for result in results:
            payload = _result_payload(result)
            texts.extend(str(value).strip() for value in payload.get("rec_texts", []))
            scores.extend(_float_values(payload.get("rec_scores", [])))
        selected_texts = [text for text in texts if text]
        warnings: list[str] = []
        if not selected_texts:
            warnings.append("ocr_no_text_detected")
        confidence = round(sum(scores) / len(scores), 6) if scores else 0.0
        if selected_texts and confidence < 0.6:
            warnings.append("ocr_low_confidence")
        return {
            "text": "\n".join(selected_texts),
            "confidence": max(0.0, min(1.0, confidence)),
            "warnings": warnings,
        }


def _result_payload(result: Any) -> dict[str, Any]:
    raw = getattr(result, "json", {})
    if callable(raw):
        raw = raw()
    if not isinstance(raw, dict):
        return {}
    nested = raw.get("res")
    return nested if isinstance(nested, dict) else raw


def _float_values(values: Iterable[Any]) -> list[float]:
    selected: list[float] = []
    for value in values:
        try:
            selected.append(float(value))
        except (TypeError, ValueError):
            continue
    return selected
