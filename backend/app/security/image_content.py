"""OCR/Vision 文本安全副本，复用通用凭证底线；不分析或改写图片像素。"""

from pydantic import ValidationError

from app.security.tool_content import ToolContentPolicy


class ImageContentRejected(RuntimeError):
    code = "image_content_rejected"
    retryable = False

    def __init__(self):
        super().__init__("图片分析内容受限。")


class ImageContentUnavailable(RuntimeError):
    code = "image_content_unavailable"
    retryable = True

    def __init__(self):
        super().__init__("图片分析内容检查暂不可用。")


def _image_view(value, *, allow_withheld=False):
    failed = False
    try:
        view = ToolContentPolicy(
            injection_action="withhold",
            max_string_chars=250_000,
            max_total_chars=2_000_000,
            max_nodes=50_000,
        ).project(value, tool_name="image-content", sink="workflow")
    except Exception:  # noqa: BLE001 - 安全故障不携带原始 provider/材料异常链。
        failed = True
    if failed:
        raise ImageContentUnavailable
    if view.metadata.disposition == "withheld" and not allow_withheld:
        raise ImageContentRejected
    return view


def protect_image_payload(value):
    return _image_view(value).data


def protect_image_result(result):
    from app.vision.schemas import OcrExtractionResult

    failed = False
    try:
        payload = result.model_dump(mode="json")
    except Exception:  # noqa: BLE001
        failed = True
    if failed:
        raise ImageContentUnavailable
    view = _image_view(payload, allow_withheld=isinstance(result, OcrExtractionResult))
    if isinstance(result, OcrExtractionResult) and view.metadata.disposition == "withheld":
        checked = OcrExtractionResult(
            text="", confidence=0.0, provider_version="content-restricted"
        )
    else:
        checked = _validated(type(result), view.data)
    if isinstance(result, OcrExtractionResult):
        checked._content_restricted = bool(
            result._content_restricted
            or view.metadata.disposition != "allowed"
            or view.metadata.risk_signals
        )
    return checked


def _validated(schema, value):
    failed = False
    try:
        result = schema.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        failed = True
    if failed:
        raise ImageContentRejected
    return result


def protect_image_record(record):
    """重建瞬态 ORM 副本；读取旧行不触发原记录的明文更新。"""
    from app.storage.models import AssetAnalysisRecord
    from app.vision.schemas import VisionAnalysis

    data = {
        column.key: getattr(record, column.key) for column in AssetAnalysisRecord.__table__.columns
    }
    fields = (
        "summary",
        "extracted_text",
        "result_json",
        "usage_json",
        "warnings_json",
        "provider",
        "model",
        "ocr_provider",
        "ocr_version",
        "vision_provider",
        "vision_version",
    )
    data.update(protect_image_payload({name: data[name] for name in fields}))
    data["result_json"] = _validated(VisionAnalysis, data["result_json"]).model_dump(mode="json")
    return AssetAnalysisRecord(**data)
