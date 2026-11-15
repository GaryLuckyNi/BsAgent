from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AssetStatus(StrEnum):
    UPLOADING = "uploading"
    PROCESSING = "processing"
    READY = "ready"
    PARTIAL = "partial"
    FAILED = "failed"
    EXPIRED = "expired"
    DELETED = "deleted"


class AssetCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset_id: str
    analysis_id: str
    tenant_id: str
    user_id: str
    session_id: str | None = None
    object_key: str
    sha256: str = Field(min_length=64, max_length=64)
    media_type: str
    size_bytes: int = Field(ge=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    status: AssetStatus = AssetStatus.UPLOADING
    expires_at: datetime


class AssetAttachment(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset_id: str
    media_type: str
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    status: AssetStatus
