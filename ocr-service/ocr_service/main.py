import asyncio
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from io import BytesIO

import anyio
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from ocr_service.config import OcrSettings, settings
from ocr_service.engine import OcrEngine, PaddleOcrEngine


ALLOWED_MEDIA_TYPES = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}


class OcrResponse(BaseModel):
    text: str = Field(max_length=200_000)
    tables: list[dict] = Field(default_factory=list, max_length=100)
    confidence: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list, max_length=100)


def create_app(
    *,
    engine_factory: Callable[[], OcrEngine] | None = None,
    api_key: str | None = None,
    config: OcrSettings | None = None,
) -> FastAPI:
    runtime = config or settings
    selected_api_key = runtime.api_key if api_key is None else api_key
    selected_factory = engine_factory or (
        lambda: PaddleOcrEngine(
            model_dir=runtime.model_dir,
            device=runtime.device,
            cpu_threads=runtime.cpu_threads,
        )
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ready = False
        app.state.engine = await anyio.to_thread.run_sync(selected_factory)
        app.state.inference_slots = asyncio.Semaphore(runtime.max_concurrency)
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False

    app = FastAPI(title="BsAgent PaddleOCR Service", lifespan=lifespan)

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        if not getattr(app.state, "ready", False):
            raise HTTPException(status_code=503, detail={"code": "model_not_ready"})
        return {"status": "ready", "model": runtime.model_name}

    @app.post("/v1/ocr", response_model=OcrResponse)
    async def ocr(
        file: UploadFile = File(),
        languages: str = Form(default="zh,en"),
        detect_tables: bool = Form(default=False),
        authorization: str | None = Header(default=None),
    ) -> OcrResponse:
        _authorize(authorization, selected_api_key)
        media_type = file.content_type or ""
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise HTTPException(
                status_code=415,
                detail={"code": "unsupported_image_type"},
            )
        try:
            image = await file.read(runtime.max_image_bytes + 1)
        finally:
            await file.close()
        if len(image) > runtime.max_image_bytes:
            raise HTTPException(status_code=413, detail={"code": "image_too_large"})
        _validate_image(
            image,
            claimed_media_type=media_type,
            max_pixels=runtime.max_image_pixels,
        )
        selected_languages = _parse_languages(languages)
        async with app.state.inference_slots:
            try:
                result = await anyio.to_thread.run_sync(
                    lambda: app.state.engine.extract(
                        image,
                        languages=selected_languages,
                    )
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "ocr_inference_failed"},
                ) from exc
        warnings = list(result.get("warnings", []))
        if detect_tables:
            warnings.append("table_detection_not_enabled")
        return OcrResponse(
            text=str(result.get("text", "")),
            tables=[],
            confidence=float(result.get("confidence", 0.0)),
            warnings=list(dict.fromkeys(warnings)),
        )

    return app


def _authorize(authorization: str | None, api_key: str) -> None:
    if not api_key:
        return
    expected = f"Bearer {api_key}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "authentication_required"},
            headers={"WWW-Authenticate": "Bearer"},
        )


def _validate_image(
    image: bytes,
    *,
    claimed_media_type: str,
    max_pixels: int,
) -> None:
    try:
        with Image.open(BytesIO(image)) as source:
            source.verify()
            actual_format = source.format
            width, height = source.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=415,
            detail={"code": "invalid_image"},
        ) from exc
    if actual_format != ALLOWED_MEDIA_TYPES[claimed_media_type]:
        raise HTTPException(
            status_code=415,
            detail={"code": "image_type_mismatch"},
        )
    if width * height > max_pixels:
        raise HTTPException(status_code=413, detail={"code": "image_too_many_pixels"})


def _parse_languages(value: str) -> tuple[str, ...]:
    selected = tuple(
        dict.fromkeys(part.strip().lower() for part in value.split(",") if part.strip())
    )
    if not selected or len(selected) > 4 or any(lang not in {"zh", "en"} for lang in selected):
        raise HTTPException(status_code=422, detail={"code": "unsupported_language"})
    return selected


app = create_app()
