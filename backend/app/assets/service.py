from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import BinaryIO, Protocol

from sqlalchemy.orm import Session

from app.assets.errors import (
    AssetAnalysisFailedError,
    AssetExpiredError,
    AssetNotFoundError,
    AssetNotReadyError,
    AssetPersistenceError,
)
from app.assets.image_security import (
    SUPPORTED_MEDIA_TYPES,
    SanitizedImage,
    sanitize_image,
)
from app.assets.object_store import ObjectStore, ObjectStoreError, validate_object_key
from app.assets.repository import SqlAlchemyAssetRepository
from app.assets.schemas import AssetAttachment, AssetCreate, AssetStatus
from app.security.asset_authorization import require_asset_owner
from app.security.principal import Principal
from app.storage.models import AssetRecord


@dataclass(frozen=True)
class AssetAnalysisDispatchRequest:
    asset_id: str
    analysis_id: str
    tenant_id: str
    user_id: str
    session_id: str | None
    object_key: str
    media_type: str
    trace_id: str | None = None


class ImageAnalysisDispatcher(Protocol):
    def dispatch(self, request: AssetAnalysisDispatchRequest) -> None: ...


class NoopImageAnalysisDispatcher:
    def dispatch(self, request: AssetAnalysisDispatchRequest) -> None:
        del request


class SqlAlchemyAssetUnitOfWork:
    def __init__(self, session: Session) -> None:
        self.assets = SqlAlchemyAssetRepository(session)
        self._session = session

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()


class AssetService:
    _REUSABLE_STATUSES = (
        AssetStatus.PROCESSING,
        AssetStatus.READY,
        AssetStatus.PARTIAL,
    )
    _EXTENSIONS = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }

    def __init__(
        self,
        *,
        uow: SqlAlchemyAssetUnitOfWork,
        object_store: ObjectStore,
        dispatcher: ImageAnalysisDispatcher,
        sanitizer: Callable[..., SanitizedImage] = sanitize_image,
        allowed_media_types: tuple[str, ...] | frozenset[str] = SUPPORTED_MEDIA_TYPES,
        max_image_bytes: int,
        max_image_pixels: int,
        retention_days: int,
        asset_id_factory: Callable[[], str],
        analysis_id_factory: Callable[[], str],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow = uow
        self._object_store = object_store
        self._dispatcher = dispatcher
        self._sanitizer = sanitizer
        self._allowed_media_types = allowed_media_types
        self._max_image_bytes = max_image_bytes
        self._max_image_pixels = max_image_pixels
        self._retention_days = retention_days
        self._asset_id_factory = asset_id_factory
        self._analysis_id_factory = analysis_id_factory
        self._now = now

    def upload_image(
        self,
        stream: BinaryIO,
        *,
        claimed_media_type: str,
        principal: Principal,
        session_id: str | None = None,
        trace_id: str | None = None,
    ) -> AssetRecord:
        sanitized = self._sanitizer(
            stream,
            claimed_media_type=claimed_media_type,
            max_image_bytes=self._max_image_bytes,
            max_image_pixels=self._max_image_pixels,
            allowed_media_types=self._allowed_media_types,
        )
        reusable = self._uow.assets.find_by_sha256(
            tenant_id=principal.tenant_id,
            sha256=sanitized.sha256,
            statuses=self._REUSABLE_STATUSES,
        )
        asset_id = self._asset_id_factory()
        analysis_id = self._analysis_id_factory()
        object_key = (
            reusable.object_key
            if reusable is not None
            else self._build_object_key(
                tenant_id=principal.tenant_id,
                asset_id=asset_id,
                media_type=sanitized.media_type,
            )
        )
        wrote_object = False
        try:
            self._uow.assets.create(
                AssetCreate(
                    asset_id=asset_id,
                    analysis_id=analysis_id,
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    session_id=session_id,
                    object_key=object_key,
                    sha256=sanitized.sha256,
                    media_type=sanitized.media_type,
                    size_bytes=len(sanitized.data),
                    width=sanitized.width,
                    height=sanitized.height,
                    status=AssetStatus.UPLOADING,
                    expires_at=self._now() + timedelta(days=self._retention_days),
                )
            )
            if reusable is None:
                self._object_store.put(
                    object_key,
                    sanitized.data,
                    media_type=sanitized.media_type,
                )
                wrote_object = True
            record = self._uow.assets.transition_status(
                asset_id=asset_id,
                tenant_id=principal.tenant_id,
                expected_status=AssetStatus.UPLOADING,
                target_status=AssetStatus.PROCESSING,
            )
            self._uow.commit()
        except Exception as exc:
            self._uow.rollback()
            if wrote_object:
                self._compensate_object_write(object_key)
            if isinstance(exc, AssetPersistenceError):
                raise
            raise AssetPersistenceError("图片资产持久化失败。") from exc

        try:
            self._dispatcher.dispatch(
                AssetAnalysisDispatchRequest(
                    asset_id=record.asset_id,
                    analysis_id=record.analysis_id,
                    tenant_id=record.tenant_id,
                    user_id=record.user_id,
                    session_id=record.session_id,
                    object_key=record.object_key,
                    media_type=record.media_type,
                    trace_id=trace_id,
                )
            )
        except Exception:
            record = self._mark_dispatch_failed(record)
        return record

    def get_asset(self, asset_id: str, *, principal: Principal) -> AssetRecord:
        record = self._uow.assets.get(asset_id, tenant_id=principal.tenant_id)
        if record is None:
            raise AssetNotFoundError("图片资产不存在。")
        require_asset_owner(record, principal, operation="read")
        if self._as_utc(record.expires_at) <= self._as_utc(self._now()):
            status = AssetStatus(record.status)
            if status != AssetStatus.EXPIRED:
                self._uow.assets.transition_status(
                    asset_id=asset_id,
                    tenant_id=principal.tenant_id,
                    expected_status=status,
                    target_status=AssetStatus.EXPIRED,
                )
                self._uow.commit()
            raise AssetExpiredError("图片资产已过期。")
        return record

    def delete_asset(self, asset_id: str, *, principal: Principal) -> bool:
        record = self._uow.assets.get(
            asset_id,
            tenant_id=principal.tenant_id,
            include_deleted=True,
        )
        if record is None or record.status == AssetStatus.DELETED:
            return False
        require_asset_owner(record, principal, operation="delete")
        try:
            deleted = self._uow.assets.soft_delete(
                asset_id=asset_id,
                tenant_id=principal.tenant_id,
            )
            self._uow.commit()
        except Exception as exc:
            self._uow.rollback()
            raise AssetPersistenceError("图片资产删除失败。") from exc
        if not deleted:
            return False
        remaining = self._uow.assets.count_live_object_references(
            tenant_id=principal.tenant_id,
            object_key=record.object_key,
        )
        if remaining == 0:
            try:
                self._object_store.delete(record.object_key)
            except ObjectStoreError as exc:
                raise AssetPersistenceError("图片对象清理失败。") from exc
        return True

    def resolve_attachments(
        self,
        asset_ids: list[str],
        *,
        principal: Principal,
        session_id: str,
    ) -> list[AssetAttachment]:
        attachments: list[AssetAttachment] = []
        for asset_id in dict.fromkeys(asset_ids):
            record = self.get_asset(asset_id, principal=principal)
            can_read_other_users = (
                "*" in principal.scopes or "asset:read:any" in principal.scopes
            )
            if not can_read_other_users and record.user_id != principal.user_id:
                raise AssetNotFoundError("图片资产不存在。")
            if record.session_id is not None and record.session_id != session_id:
                raise AssetNotFoundError("图片资产不存在。")
            status = AssetStatus(record.status)
            if status == AssetStatus.FAILED:
                raise AssetAnalysisFailedError("图片资产分析失败。")
            if status not in {AssetStatus.READY, AssetStatus.PARTIAL}:
                raise AssetNotReadyError("图片资产尚未完成分析。")
            attachments.append(
                AssetAttachment(
                    asset_id=record.asset_id,
                    media_type=record.media_type,
                    width=record.width,
                    height=record.height,
                    status=status,
                )
            )
        return attachments

    def _mark_dispatch_failed(self, record: AssetRecord) -> AssetRecord:
        try:
            failed = self._uow.assets.transition_status(
                asset_id=record.asset_id,
                tenant_id=record.tenant_id,
                expected_status=AssetStatus.PROCESSING,
                target_status=AssetStatus.FAILED,
                failure_code="analysis_dispatch_failed",
            )
            self._uow.commit()
            return failed
        except Exception as exc:
            self._uow.rollback()
            raise AssetPersistenceError("图片分析任务派发失败且状态更新失败。") from exc

    def _compensate_object_write(self, object_key: str) -> None:
        try:
            self._object_store.delete(object_key)
        except ObjectStoreError:
            return

    @classmethod
    def _build_object_key(
        cls,
        *,
        tenant_id: str,
        asset_id: str,
        media_type: str,
    ) -> str:
        tenant_namespace = sha256(tenant_id.encode("utf-8")).hexdigest()[:24]
        extension = cls._EXTENSIONS[media_type]
        return validate_object_key(
            f"tenants/{tenant_namespace}/assets/{asset_id}.{extension}"
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
