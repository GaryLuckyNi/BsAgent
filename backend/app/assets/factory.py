"""图片资产运行时依赖工厂。"""

from typing import Any
from uuid import uuid4

import boto3
from botocore.config import Config as BotoConfig
from sqlalchemy.orm import Session

from app.assets.local_store import LocalObjectStore
from app.assets.object_store import ObjectStore
from app.assets.s3_store import S3ObjectStore
from app.assets.service import (
    AssetService,
    ImageAnalysisDispatcher,
    NoopImageAnalysisDispatcher,
    SqlAlchemyAssetUnitOfWork,
)
from app.core.config import Settings, settings


def build_object_store(
    *,
    config: Settings | None = None,
    s3_client: Any | None = None,
) -> ObjectStore:
    runtime = config or settings
    if runtime.asset_store_backend == "local":
        return LocalObjectStore(runtime.asset_local_root)

    client = s3_client or boto3.client(
        "s3",
        endpoint_url=runtime.asset_s3_endpoint_url,
        region_name=runtime.asset_s3_region,
        config=BotoConfig(
            connect_timeout=3,
            read_timeout=15,
            retries={"mode": "adaptive", "max_attempts": 4},
            signature_version="s3v4",
        ),
    )
    return S3ObjectStore(
        client=client,
        bucket=runtime.asset_s3_bucket or "",
        server_side_encryption=runtime.asset_s3_server_side_encryption,
        kms_key_id=runtime.asset_s3_kms_key_id,
    )


def build_asset_service(
    *,
    session: Session,
    dispatcher: ImageAnalysisDispatcher,
    config: Settings | None = None,
    object_store: ObjectStore | None = None,
) -> AssetService:
    runtime = config or settings
    if runtime.app_env == "production" and isinstance(
        dispatcher,
        NoopImageAnalysisDispatcher,
    ):
        raise ValueError("生产环境禁止使用 NoopImageAnalysisDispatcher。")
    return AssetService(
        uow=SqlAlchemyAssetUnitOfWork(session),
        object_store=object_store or build_object_store(config=runtime),
        dispatcher=dispatcher,
        max_image_bytes=runtime.max_image_bytes,
        max_image_pixels=runtime.max_image_pixels,
        allowed_media_types=runtime.image_allowed_media_types,
        retention_days=runtime.asset_retention_days,
        asset_id_factory=lambda: uuid4().hex,
        analysis_id_factory=lambda: uuid4().hex,
    )
