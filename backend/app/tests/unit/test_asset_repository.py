import importlib.util
from importlib import import_module
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.assets.repository import AssetStateConflictError, SqlAlchemyAssetRepository
from app.assets.schemas import AssetCreate, AssetStatus
from app.storage.models import Base


def test_asset_repository_modules_are_available():
    assert importlib.util.find_spec("app.assets.schemas") is not None
    assert importlib.util.find_spec("app.assets.repository") is not None


def test_asset_repository_exposes_domain_contract():
    schemas = import_module("app.assets.schemas")
    repository = import_module("app.assets.repository")

    assert getattr(schemas, "AssetCreate", None) is not None
    assert getattr(schemas, "AssetStatus", None) is not None
    assert getattr(repository, "SqlAlchemyAssetRepository", None) is not None
    assert getattr(repository, "AssetStateConflictError", None) is not None


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _asset(
    *,
    asset_id="asset-1",
    tenant_id="tenant-1",
    sha256="a" * 64,
    status=AssetStatus.UPLOADING,
    expires_at=None,
):
    return AssetCreate(
        asset_id=asset_id,
        analysis_id=f"analysis-{asset_id}",
        tenant_id=tenant_id,
        user_id="user-1",
        session_id="session-1",
        object_key=f"tenants/{tenant_id}/assets/{asset_id}.png",
        sha256=sha256,
        media_type="image/png",
        size_bytes=128,
        width=16,
        height=8,
        status=status,
        expires_at=expires_at or datetime.now(UTC) + timedelta(days=7),
    )


def test_asset_repository_never_reads_another_tenants_asset(session):
    repository = SqlAlchemyAssetRepository(session)
    repository.create(_asset())

    assert repository.get("asset-1", tenant_id="tenant-1") is not None
    assert repository.get("asset-1", tenant_id="tenant-2") is None


def test_asset_repository_finds_reusable_content_only_inside_tenant(session):
    repository = SqlAlchemyAssetRepository(session)
    repository.create(
        _asset(
            asset_id="asset-ready",
            tenant_id="tenant-1",
            status=AssetStatus.READY,
        )
    )
    repository.create(
        _asset(
            asset_id="asset-other-tenant",
            tenant_id="tenant-2",
            status=AssetStatus.READY,
        )
    )

    reusable = repository.find_by_sha256(
        tenant_id="tenant-1",
        sha256="a" * 64,
        statuses=(AssetStatus.PROCESSING, AssetStatus.READY),
    )

    assert reusable.asset_id == "asset-ready"
    assert repository.find_by_sha256(
        tenant_id="tenant-3",
        sha256="a" * 64,
        statuses=(AssetStatus.READY,),
    ) is None


def test_asset_repository_lists_only_live_expired_records(session):
    repository = SqlAlchemyAssetRepository(session)
    now = datetime.now(UTC)
    repository.create(
        _asset(asset_id="asset-expired", expires_at=now - timedelta(seconds=1))
    )
    repository.create(
        _asset(asset_id="asset-future", expires_at=now + timedelta(days=1))
    )
    repository.create(
        _asset(
            asset_id="asset-deleted",
            status=AssetStatus.DELETED,
            expires_at=now - timedelta(days=1),
        )
    )

    records = repository.list_expired(before=now, limit=10)

    assert [record.asset_id for record in records] == ["asset-expired"]


def test_asset_repository_transitions_status_with_compare_and_set(session):
    repository = SqlAlchemyAssetRepository(session)
    repository.create(_asset())

    transitioned = repository.transition_status(
        asset_id="asset-1",
        tenant_id="tenant-1",
        expected_status=AssetStatus.UPLOADING,
        target_status=AssetStatus.PROCESSING,
    )

    assert transitioned.status == AssetStatus.PROCESSING
    with pytest.raises(AssetStateConflictError):
        repository.transition_status(
            asset_id="asset-1",
            tenant_id="tenant-1",
            expected_status=AssetStatus.UPLOADING,
            target_status=AssetStatus.FAILED,
        )


def test_asset_repository_rejects_transition_not_in_domain_matrix(session):
    repository = SqlAlchemyAssetRepository(session)
    repository.create(_asset())

    with pytest.raises(AssetStateConflictError):
        repository.transition_status(
            asset_id="asset-1",
            tenant_id="tenant-1",
            expected_status=AssetStatus.UPLOADING,
            target_status=AssetStatus.READY,
        )


def test_asset_repository_soft_delete_is_idempotent_and_hidden_by_default(session):
    repository = SqlAlchemyAssetRepository(session)
    repository.create(_asset(status=AssetStatus.READY))

    assert repository.soft_delete(asset_id="asset-1", tenant_id="tenant-1") is True
    assert repository.soft_delete(asset_id="asset-1", tenant_id="tenant-1") is False
    assert repository.get("asset-1", tenant_id="tenant-1") is None
    deleted = repository.get(
        "asset-1",
        tenant_id="tenant-1",
        include_deleted=True,
    )
    assert deleted.status == AssetStatus.DELETED
    assert deleted.deleted_at is not None


def test_asset_repository_counts_live_object_references_inside_tenant(session):
    repository = SqlAlchemyAssetRepository(session)
    first = _asset(asset_id="asset-1", status=AssetStatus.READY)
    second = _asset(asset_id="asset-2", status=AssetStatus.PROCESSING)
    second = second.model_copy(update={"object_key": first.object_key})
    repository.create(first)
    repository.create(second)
    repository.create(
        _asset(
            asset_id="asset-other-tenant",
            tenant_id="tenant-2",
            status=AssetStatus.READY,
        ).model_copy(update={"object_key": first.object_key})
    )

    assert repository.count_live_object_references(
        tenant_id="tenant-1",
        object_key=first.object_key,
    ) == 2

    repository.soft_delete(asset_id="asset-1", tenant_id="tenant-1")

    assert repository.count_live_object_references(
        tenant_id="tenant-1",
        object_key=first.object_key,
    ) == 1
