from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.assets.object_store import ObjectStore
from app.assets.repository import (
    ALLOWED_STATUS_TRANSITIONS,
    SqlAlchemyAssetRepository,
)
from app.assets.schemas import AssetStatus


class AssetRetentionService:
    def __init__(
        self,
        *,
        session: Session,
        object_store: ObjectStore,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session = session
        self._repository = SqlAlchemyAssetRepository(session)
        self._object_store = object_store
        self._now = now

    def purge_expired(self, *, limit: int) -> int:
        records = self._repository.list_expired(before=self._now(), limit=limit)
        deleted = 0
        for record in records:
            try:
                self._purge_one(record)
                self._session.commit()
            except Exception:
                self._session.rollback()
                raise
            deleted += 1
        return deleted

    def _purge_one(self, record) -> None:
        for artifact_key in self._repository.list_analysis_artifact_keys(
            asset_id=record.asset_id,
            tenant_id=record.tenant_id,
        ):
            self._object_store.delete(artifact_key)

        remaining_references = self._repository.count_live_object_references(
            tenant_id=record.tenant_id,
            object_key=record.object_key,
            excluding_asset_id=record.asset_id,
        )
        if remaining_references == 0:
            self._object_store.delete(record.object_key)

        self._repository.delete_analysis_records(
            asset_id=record.asset_id,
            tenant_id=record.tenant_id,
        )
        current_status = AssetStatus(record.status)
        if AssetStatus.EXPIRED in ALLOWED_STATUS_TRANSITIONS[current_status]:
            self._repository.transition_status(
                asset_id=record.asset_id,
                tenant_id=record.tenant_id,
                expected_status=current_status,
                target_status=AssetStatus.EXPIRED,
            )
            current_status = AssetStatus.EXPIRED
        self._repository.transition_status(
            asset_id=record.asset_id,
            tenant_id=record.tenant_id,
            expected_status=current_status,
            target_status=AssetStatus.DELETED,
        )
