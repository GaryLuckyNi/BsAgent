from typing import Annotated
from datetime import datetime

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.assets.errors import (
    AssetAnalysisFailedError,
    AssetExpiredError,
    AssetNotFoundError,
    AssetNotReadyError,
    AssetPersistenceError,
    AssetValidationError,
)
from app.assets.factory import build_asset_service, build_object_store
from app.assets.schemas import AssetStatus
from app.assets.service import AssetService
from app.core.config import settings
from app.operations.rate_limit import RateLimitPolicy, require_rate_limit
from app.security.dependencies import require_permission
from app.security.principal import Principal
from app.storage.database import get_db
from app.vision.factory import build_image_analysis_dispatcher
from app.vision.repository import SqlAlchemyVisionAnalysisRepository
from app.vision.schemas import VisionAnalysis

router = APIRouter(prefix="/api/assets", tags=["assets"])
IMAGE_UPLOAD_RATE_LIMIT = RateLimitPolicy(
    name="image_upload",
    requests_per_minute=settings.image_upload_rate_limit_per_minute,
)


class AssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_id: str
    media_type: str
    size_bytes: int
    width: int
    height: int
    status: AssetStatus
    failure_code: str | None
    expires_at: datetime


class AssetAnalysisResponse(BaseModel):
    asset_id: str
    schema_version: str
    provider: str
    model: str
    analysis: VisionAnalysis
    created_at: datetime


def get_asset_service(
    request: Request,
    db: Session = Depends(get_db),
) -> AssetService:
    object_store = getattr(request.app.state, "asset_object_store", None)
    if object_store is None:
        object_store = build_object_store()
        request.app.state.asset_object_store = object_store
    dispatcher = getattr(request.app.state, "image_analysis_dispatcher", None)
    if dispatcher is None:
        dispatcher = build_image_analysis_dispatcher()
        request.app.state.image_analysis_dispatcher = dispatcher
    return build_asset_service(
        session=db,
        dispatcher=dispatcher,
        object_store=object_store,
    )


def get_vision_analysis_repository(
    db: Session = Depends(get_db),
) -> SqlAlchemyVisionAnalysisRepository:
    return SqlAlchemyVisionAnalysisRepository(db)


@router.post(
    "/images",
    response_model=AssetResponse,
    status_code=202,
    dependencies=[Depends(require_rate_limit(IMAGE_UPLOAD_RATE_LIMIT))],
)
def upload_image(
    file: Annotated[UploadFile, File()],
    session_id: Annotated[str | None, Form()] = None,
    principal: Principal = Depends(require_permission("asset:write")),
    service: AssetService = Depends(get_asset_service),
) -> AssetResponse:
    try:
        record = service.upload_image(
            file.file,
            claimed_media_type=file.content_type or "",
            principal=principal,
            session_id=session_id,
        )
    except Exception as exc:
        raise to_asset_http_error(exc) from exc
    finally:
        file.file.close()
    return AssetResponse.model_validate(record)


@router.get("/{asset_id}", response_model=AssetResponse)
def get_asset(
    asset_id: str,
    principal: Principal = Depends(require_permission("asset:read")),
    service: AssetService = Depends(get_asset_service),
) -> AssetResponse:
    try:
        record = service.get_asset(asset_id, principal=principal)
    except Exception as exc:
        raise to_asset_http_error(exc) from exc
    return AssetResponse.model_validate(record)


@router.get("/{asset_id}/analysis", response_model=AssetAnalysisResponse)
def get_asset_analysis(
    asset_id: str,
    principal: Principal = Depends(require_permission("asset:read")),
    service: AssetService = Depends(get_asset_service),
    repository: SqlAlchemyVisionAnalysisRepository = Depends(
        get_vision_analysis_repository
    ),
) -> AssetAnalysisResponse:
    try:
        service.get_asset(asset_id, principal=principal)
        record = repository.get_latest(
            asset_id=asset_id,
            tenant_id=principal.tenant_id,
        )
        if record is None:
            raise AssetNotReadyError("图片资产尚未完成分析。")
        return AssetAnalysisResponse(
            asset_id=record.asset_id,
            schema_version=record.schema_version,
            provider=record.provider,
            model=record.model,
            analysis=VisionAnalysis.model_validate(record.result_json),
            created_at=record.created_at,
        )
    except Exception as exc:
        raise to_asset_http_error(exc) from exc


@router.delete("/{asset_id}", status_code=204)
def delete_asset(
    asset_id: str,
    principal: Principal = Depends(require_permission("asset:delete")),
    service: AssetService = Depends(get_asset_service),
) -> Response:
    try:
        service.delete_asset(asset_id, principal=principal)
    except Exception as exc:
        raise to_asset_http_error(exc) from exc
    return Response(status_code=204)


def to_asset_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AssetValidationError):
        status_code = {
            "asset_too_large": 413,
            "unsupported_image_type": 415,
        }.get(exc.code, 400)
        return HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, AssetNotFoundError):
        return HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": "图片资产不存在。"},
        )
    if isinstance(exc, AssetExpiredError):
        return HTTPException(
            status_code=404,
            detail={"code": "asset_not_found", "message": "图片资产不存在。"},
        )
    if isinstance(exc, AssetNotReadyError):
        return HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": "图片资产尚未完成分析。"},
        )
    if isinstance(exc, AssetAnalysisFailedError):
        return HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": "图片资产分析失败，请重新上传。"},
        )
    if isinstance(exc, AssetPersistenceError):
        return HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": "图片资产服务暂时不可用。"},
        )
    return HTTPException(
        status_code=500,
        detail={"code": "asset_service_error", "message": "图片资产处理失败。"},
    )
