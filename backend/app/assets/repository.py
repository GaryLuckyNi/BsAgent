from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.assets.schemas import AssetCreate, AssetStatus
from app.storage.models import AssetAnalysisRecord, AssetRecord


ALLOWED_STATUS_TRANSITIONS: dict[AssetStatus, frozenset[AssetStatus]] = {
    AssetStatus.UPLOADING: frozenset(
        {AssetStatus.PROCESSING, AssetStatus.FAILED, AssetStatus.DELETED}
    ),
    AssetStatus.PROCESSING: frozenset(
        {
            AssetStatus.READY,
            AssetStatus.PARTIAL,
            AssetStatus.FAILED,
            AssetStatus.EXPIRED,
            AssetStatus.DELETED,
        }
    ),
    AssetStatus.READY: frozenset({AssetStatus.EXPIRED, AssetStatus.DELETED}),
    AssetStatus.PARTIAL: frozenset(
        {
            AssetStatus.PROCESSING,
            AssetStatus.READY,
            AssetStatus.FAILED,
            AssetStatus.EXPIRED,
            AssetStatus.DELETED,
        }
    ),
    AssetStatus.FAILED: frozenset(
        {AssetStatus.PROCESSING, AssetStatus.EXPIRED, AssetStatus.DELETED}
    ),
    AssetStatus.EXPIRED: frozenset({AssetStatus.DELETED}),
    AssetStatus.DELETED: frozenset(),
}


class AssetStateConflictError(RuntimeError):
    code = "asset_state_conflict"


class SqlAlchemyAssetRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, data: AssetCreate) -> AssetRecord:
        record = AssetRecord(**data.model_dump(mode="python"))
        self._session.add(record)
        self._session.flush()
        return record

    def get(
        self,
        asset_id: str,
        *,
        tenant_id: str,
        include_deleted: bool = False,
    ) -> AssetRecord | None:
        statement = select(AssetRecord).where(
            AssetRecord.asset_id == asset_id,
            AssetRecord.tenant_id == tenant_id,
        )
        if not include_deleted:
            statement = statement.where(AssetRecord.status != AssetStatus.DELETED.value)
        return self._session.scalar(statement)

    def find_by_sha256(
        self,
        *,
        tenant_id: str,
        sha256: str,
        statuses: tuple[AssetStatus, ...],
    ) -> AssetRecord | None:
        if not statuses:
            return None
        return self._session.scalar(
            select(AssetRecord)
            .where(
                AssetRecord.tenant_id == tenant_id,
                AssetRecord.sha256 == sha256,
                AssetRecord.status.in_(status.value for status in statuses),
                AssetRecord.deleted_at.is_(None),
            )
            .order_by(AssetRecord.created_at, AssetRecord.asset_id)
            .limit(1)
        )

    def list_expired(
        self,
        *,
        before: datetime,
        limit: int = 100,
    ) -> list[AssetRecord]:
        return list(
            self._session.scalars(
                select(AssetRecord)
                .where(
                    AssetRecord.expires_at <= before,
                    AssetRecord.status.not_in(
                        (AssetStatus.EXPIRED.value, AssetStatus.DELETED.value)
                    ),
                    AssetRecord.deleted_at.is_(None),
                )
                .order_by(AssetRecord.expires_at, AssetRecord.asset_id)
                .limit(limit)
            )
        )

    def count_live_object_references(
        self,
        *,
        tenant_id: str,
        object_key: str,
        excluding_asset_id: str | None = None,
    ) -> int:
        statement = select(func.count(AssetRecord.asset_id)).where(
                AssetRecord.tenant_id == tenant_id,
                AssetRecord.object_key == object_key,
                AssetRecord.status != AssetStatus.DELETED.value,
                AssetRecord.deleted_at.is_(None),
            )
        if excluding_asset_id is not None:
            statement = statement.where(AssetRecord.asset_id != excluding_asset_id)
        count = self._session.scalar(statement)
        return int(count or 0)

    def list_analysis_artifact_keys(
        self,
        *,
        asset_id: str,
        tenant_id: str,
    ) -> list[str]:
        return list(
            self._session.scalars(
                select(AssetAnalysisRecord.artifact_storage_key).where(
                    AssetAnalysisRecord.asset_id == asset_id,
                    AssetAnalysisRecord.tenant_id == tenant_id,
                    AssetAnalysisRecord.artifact_storage_key.is_not(None),
                )
            )
        )

    def delete_analysis_records(self, *, asset_id: str, tenant_id: str) -> int:
        result = self._session.execute(
            delete(AssetAnalysisRecord).where(
                AssetAnalysisRecord.asset_id == asset_id,
                AssetAnalysisRecord.tenant_id == tenant_id,
            )
        )
        self._session.flush()
        return int(result.rowcount or 0)

    def transition_status(
        self,
        *,
        asset_id: str,
        tenant_id: str,
        expected_status: AssetStatus,
        target_status: AssetStatus,
        failure_code: str | None = None,
    ) -> AssetRecord:
        if target_status not in ALLOWED_STATUS_TRANSITIONS[expected_status]:
            raise AssetStateConflictError(
                f"不允许从 {expected_status.value} 迁移到 {target_status.value}。"
            )
        now = datetime.now(UTC)
        values = {
            "status": target_status.value,
            "failure_code": failure_code,
            "updated_at": now,
        }
        if target_status == AssetStatus.DELETED:
            values["deleted_at"] = now
        result = self._session.execute(
            update(AssetRecord)
            .where(
                AssetRecord.asset_id == asset_id,
                AssetRecord.tenant_id == tenant_id,
                AssetRecord.status == expected_status.value,
                AssetRecord.deleted_at.is_(None),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise AssetStateConflictError("图片资产状态已变化或资产不存在。")
        self._session.flush()
        self._session.expire_all()
        record = self.get(asset_id, tenant_id=tenant_id, include_deleted=True)
        if record is None:
            raise AssetStateConflictError("图片资产状态更新后不可见。")
        return record

    def soft_delete(self, *, asset_id: str, tenant_id: str) -> bool:
        now = datetime.now(UTC)
        result = self._session.execute(
            update(AssetRecord)
            .where(
                AssetRecord.asset_id == asset_id,
                AssetRecord.tenant_id == tenant_id,
                AssetRecord.status != AssetStatus.DELETED.value,
                AssetRecord.deleted_at.is_(None),
            )
            .values(
                status=AssetStatus.DELETED.value,
                deleted_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        self._session.expire_all()
        return result.rowcount == 1
