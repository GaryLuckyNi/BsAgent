from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.assets.repository import SqlAlchemyAssetRepository
from app.assets.retention import AssetRetentionService
from app.assets.schemas import AssetCreate, AssetStatus
from app.storage.models import AssetAnalysisRecord, Base


class RecordingObjectStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _asset(*, asset_id: str, object_key: str, expires_at: datetime) -> AssetCreate:
    return AssetCreate(
        asset_id=asset_id,
        analysis_id=f"analysis-{asset_id}",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        object_key=object_key,
        sha256="a" * 64,
        media_type="image/png",
        size_bytes=10,
        width=1,
        height=1,
        status=AssetStatus.READY,
        expires_at=expires_at,
    )


def test_retention_deletes_expired_asset_and_analysis_artifact(session):
    now = datetime(2026, 7, 25, tzinfo=UTC)
    repository = SqlAlchemyAssetRepository(session)
    repository.create(
        _asset(
            asset_id="asset-expired",
            object_key="tenants/t/assets/shared.png",
            expires_at=now - timedelta(seconds=1),
        )
    )
    session.add(
        AssetAnalysisRecord(
            analysis_id="analysis-asset-expired",
            asset_id="asset-expired",
            tenant_id="tenant-1",
            schema_version="1.0",
            provider="vision",
            model="model",
            summary="summary",
            extracted_text="text",
            result_json={},
            usage_json={},
            confidence=0.9,
            artifact_storage_key="tenants/t/analysis/analysis-expired.json",
        )
    )
    session.commit()
    object_store = RecordingObjectStore()

    deleted = AssetRetentionService(
        session=session,
        object_store=object_store,
        now=lambda: now,
    ).purge_expired(limit=100)

    assert deleted == 1
    assert object_store.deleted == [
        "tenants/t/analysis/analysis-expired.json",
        "tenants/t/assets/shared.png",
    ]
    record = repository.get(
        "asset-expired",
        tenant_id="tenant-1",
        include_deleted=True,
    )
    assert record is not None
    assert record.status == AssetStatus.DELETED.value
    assert repository.list_analysis_artifact_keys(
        asset_id="asset-expired",
        tenant_id="tenant-1",
    ) == []


def test_retention_keeps_shared_object_until_last_reference_expires(session):
    now = datetime(2026, 7, 25, tzinfo=UTC)
    repository = SqlAlchemyAssetRepository(session)
    repository.create(
        _asset(
            asset_id="asset-expired",
            object_key="tenants/t/assets/shared.png",
            expires_at=now - timedelta(seconds=1),
        )
    )
    repository.create(
        _asset(
            asset_id="asset-live",
            object_key="tenants/t/assets/shared.png",
            expires_at=now + timedelta(days=1),
        )
    )
    session.commit()
    object_store = RecordingObjectStore()

    deleted = AssetRetentionService(
        session=session,
        object_store=object_store,
        now=lambda: now,
    ).purge_expired(limit=100)

    assert deleted == 1
    assert "tenants/t/assets/shared.png" not in object_store.deleted
