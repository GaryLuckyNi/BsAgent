import importlib.util
from importlib import import_module
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.assets.errors import (
    AssetAnalysisFailedError,
    AssetExpiredError,
    AssetNotFoundError,
    AssetNotReadyError,
    AssetPersistenceError,
)
from app.assets.image_security import SanitizedImage
from app.assets.object_store import ObjectNotFoundError, ObjectStoreError
from app.assets.schemas import AssetStatus
from app.assets.service import AssetService, SqlAlchemyAssetUnitOfWork
from app.security.principal import Principal
from app.storage.models import AssetRecord, Base


def test_asset_service_modules_are_available():
    assert importlib.util.find_spec("app.assets.service") is not None
    assert importlib.util.find_spec("app.assets.factory") is not None


def test_asset_service_exposes_transaction_and_dispatch_contracts():
    service = import_module("app.assets.service")

    assert getattr(service, "AssetService", None) is not None
    assert getattr(service, "SqlAlchemyAssetUnitOfWork", None) is not None
    assert getattr(service, "ImageAnalysisDispatcher", None) is not None
    assert getattr(service, "NoopImageAnalysisDispatcher", None) is not None


class RecordingObjectStore:
    def __init__(self, *, fail_put=False):
        self.fail_put = fail_put
        self.objects = {}
        self.put_calls = []
        self.deleted_keys = []

    def put(self, key, data, *, media_type):
        self.put_calls.append((key, data, media_type))
        if self.fail_put:
            raise ObjectStoreError("write failed")
        self.objects[key] = data

    def get(self, key):
        if key not in self.objects:
            raise ObjectNotFoundError("missing")
        return self.objects[key]

    def delete(self, key):
        self.deleted_keys.append(key)
        self.objects.pop(key, None)


class RecordingDispatcher:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.requests = []

    def dispatch(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("queue unavailable")


class FailingCommitUnitOfWork(SqlAlchemyAssetUnitOfWork):
    def commit(self):
        raise RuntimeError("database commit failed")


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _principal(*, tenant_id="tenant-1", user_id="user-1"):
    return Principal(
        tenant_id=tenant_id,
        user_id=user_id,
        scopes=("asset:write", "asset:read"),
    )


def _sanitizer(*_, **__):
    data = b"normalized-image"
    return SanitizedImage(
        data=data,
        media_type="image/png",
        width=32,
        height=16,
        sha256=sha256(data).hexdigest(),
    )


def _service(
    session,
    *,
    store=None,
    dispatcher=None,
    uow=None,
    ids=None,
    now=None,
):
    id_list = list(ids or ["asset-1"])
    id_values = iter(id_list)
    analysis_id_values = iter(f"analysis-{asset_id}" for asset_id in id_list)
    return AssetService(
        uow=uow or SqlAlchemyAssetUnitOfWork(session),
        object_store=store or RecordingObjectStore(),
        dispatcher=dispatcher or RecordingDispatcher(),
        sanitizer=_sanitizer,
        max_image_bytes=1024,
        max_image_pixels=1_000_000,
        retention_days=7,
        asset_id_factory=lambda: next(id_values),
        analysis_id_factory=lambda: next(analysis_id_values),
        now=now or (lambda: datetime.now(UTC)),
    )


def test_asset_service_uploads_private_normalized_object_and_dispatches(session):
    store = RecordingObjectStore()
    dispatcher = RecordingDispatcher()
    service = _service(session, store=store, dispatcher=dispatcher)

    record = service.upload_image(
        BytesIO(b"untrusted-upload"),
        claimed_media_type="image/png",
        principal=_principal(),
        session_id="session-1",
    )

    assert record.status == AssetStatus.PROCESSING
    assert record.tenant_id == "tenant-1"
    assert record.user_id == "user-1"
    assert record.analysis_id == "analysis-asset-1"
    assert store.put_calls == [
        (record.object_key, b"normalized-image", "image/png")
    ]
    assert dispatcher.requests[0].asset_id == record.asset_id
    assert dispatcher.requests[0].tenant_id == "tenant-1"


def test_asset_service_deduplicates_binary_only_inside_same_tenant(session):
    store = RecordingObjectStore()
    dispatcher = RecordingDispatcher()
    service = _service(
        session,
        store=store,
        dispatcher=dispatcher,
        ids=["asset-1", "asset-2", "asset-3"],
    )

    first = service.upload_image(
        BytesIO(b"first"),
        claimed_media_type="image/png",
        principal=_principal(tenant_id="tenant-1"),
    )
    second = service.upload_image(
        BytesIO(b"second"),
        claimed_media_type="image/png",
        principal=_principal(tenant_id="tenant-1"),
    )
    third = service.upload_image(
        BytesIO(b"third"),
        claimed_media_type="image/png",
        principal=_principal(tenant_id="tenant-2"),
    )

    assert first.asset_id != second.asset_id
    assert first.object_key == second.object_key
    assert third.object_key != first.object_key
    assert len(store.put_calls) == 2
    assert len(dispatcher.requests) == 3


def test_asset_service_rolls_back_metadata_when_object_write_fails(session):
    service = _service(session, store=RecordingObjectStore(fail_put=True))

    with pytest.raises(AssetPersistenceError):
        service.upload_image(
            BytesIO(b"source"),
            claimed_media_type="image/png",
            principal=_principal(),
        )

    assert session.scalars(select(AssetRecord)).all() == []


def test_asset_service_compensates_object_when_database_commit_fails(session):
    store = RecordingObjectStore()
    uow = FailingCommitUnitOfWork(session)
    service = _service(session, store=store, uow=uow)

    with pytest.raises(AssetPersistenceError):
        service.upload_image(
            BytesIO(b"source"),
            claimed_media_type="image/png",
            principal=_principal(),
        )

    assert store.deleted_keys == [store.put_calls[0][0]]
    assert session.scalars(select(AssetRecord)).all() == []


def test_asset_service_marks_saved_asset_failed_when_dispatch_fails(session):
    store = RecordingObjectStore()
    dispatcher = RecordingDispatcher(fail=True)
    service = _service(session, store=store, dispatcher=dispatcher)

    record = service.upload_image(
        BytesIO(b"source"),
        claimed_media_type="image/png",
        principal=_principal(),
    )

    assert record.status == AssetStatus.FAILED
    assert record.failure_code == "analysis_dispatch_failed"
    assert record.object_key in store.objects


def test_asset_service_get_is_tenant_scoped_and_expires_stale_asset(session):
    now = datetime.now(UTC)
    service = _service(session, now=lambda: now)
    record = service.upload_image(
        BytesIO(b"source"),
        claimed_media_type="image/png",
        principal=_principal(),
    )
    record.expires_at = now - timedelta(seconds=1)
    session.commit()

    with pytest.raises(AssetNotFoundError):
        service.get_asset(record.asset_id, principal=_principal(tenant_id="tenant-2"))
    with pytest.raises(AssetExpiredError):
        service.get_asset(record.asset_id, principal=_principal())

    expired = session.get(AssetRecord, record.asset_id)
    assert expired.status == AssetStatus.EXPIRED


def test_asset_service_deletes_shared_object_only_after_last_live_reference(session):
    store = RecordingObjectStore()
    service = _service(
        session,
        store=store,
        ids=["asset-1", "asset-2"],
    )
    first = service.upload_image(
        BytesIO(b"first"),
        claimed_media_type="image/png",
        principal=_principal(),
    )
    second = service.upload_image(
        BytesIO(b"second"),
        claimed_media_type="image/png",
        principal=_principal(),
    )

    assert service.delete_asset(first.asset_id, principal=_principal()) is True
    assert store.deleted_keys == []
    assert service.delete_asset(second.asset_id, principal=_principal()) is True
    assert store.deleted_keys == [first.object_key]
    assert service.delete_asset(second.asset_id, principal=_principal()) is False


def test_asset_service_resolves_only_ready_owned_session_attachments(session):
    service = _service(session)
    record = service.upload_image(
        BytesIO(b"source"),
        claimed_media_type="image/png",
        principal=_principal(),
        session_id="session-1",
    )

    with pytest.raises(AssetNotReadyError):
        service.resolve_attachments(
            [record.asset_id],
            principal=_principal(),
            session_id="session-1",
        )

    service._uow.assets.transition_status(
        asset_id=record.asset_id,
        tenant_id="tenant-1",
        expected_status=AssetStatus.PROCESSING,
        target_status=AssetStatus.READY,
    )
    service._uow.commit()

    attachments = service.resolve_attachments(
        [record.asset_id],
        principal=_principal(),
        session_id="session-1",
    )

    assert attachments[0].asset_id == record.asset_id
    assert attachments[0].media_type == "image/png"
    with pytest.raises(AssetNotFoundError):
        service.resolve_attachments(
            [record.asset_id],
            principal=_principal(user_id="other-user"),
            session_id="session-1",
        )
    with pytest.raises(AssetNotFoundError):
        service.resolve_attachments(
            [record.asset_id],
            principal=_principal(),
            session_id="other-session",
        )


def test_asset_service_reports_failed_analysis_separately_from_processing(session):
    service = _service(session)
    record = service.upload_image(
        BytesIO(b"source"),
        claimed_media_type="image/png",
        principal=_principal(),
        session_id="session-1",
    )
    service._uow.assets.transition_status(
        asset_id=record.asset_id,
        tenant_id="tenant-1",
        expected_status=AssetStatus.PROCESSING,
        target_status=AssetStatus.FAILED,
        failure_code="image_analysis_unavailable",
    )
    service._uow.commit()

    with pytest.raises(AssetAnalysisFailedError) as exc_info:
        service.resolve_attachments(
            [record.asset_id],
            principal=_principal(),
            session_id="session-1",
        )

    assert exc_info.value.code == "asset_analysis_failed"
    assert "image_analysis_unavailable" not in str(exc_info.value)
