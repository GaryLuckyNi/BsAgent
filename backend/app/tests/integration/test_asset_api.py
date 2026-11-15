from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api import assets
from app.assets.errors import AssetNotFoundError, AssetValidationError
from app.assets.schemas import AssetStatus
from app.main import create_app
from app.security.dependencies import get_current_principal
from app.security.principal import Principal
from app.vision.schemas import VisionAnalysis


def _record(**overrides):
    values = {
        "asset_id": "asset-1",
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "object_key": "private/internal/key.png",
        "sha256": "a" * 64,
        "media_type": "image/png",
        "size_bytes": 128,
        "width": 32,
        "height": 16,
        "status": AssetStatus.PROCESSING,
        "failure_code": None,
        "expires_at": datetime.now(UTC) + timedelta(days=7),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RecordingAssetService:
    def __init__(self):
        self.uploads = []
        self.deleted = []
        self.upload_error = None
        self.get_error = None

    def upload_image(self, stream, **kwargs):
        if self.upload_error is not None:
            raise self.upload_error
        self.uploads.append((stream.read(), kwargs))
        return _record(session_id=kwargs["session_id"])

    def get_asset(self, asset_id, *, principal):
        if self.get_error is not None:
            raise self.get_error
        assert principal.tenant_id == "tenant-1"
        return _record(asset_id=asset_id)

    def delete_asset(self, asset_id, *, principal):
        self.deleted.append((asset_id, principal.tenant_id))
        return True


def _client(
    service,
    fake_business_intent_catalog_runtime,
    *,
    scopes=("asset:write", "asset:read", "asset:delete"),
):
    app = create_app(business_intent_catalog_runtime=fake_business_intent_catalog_runtime)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        tenant_id="tenant-1",
        user_id="user-1",
        scopes=scopes,
    )
    app.dependency_overrides[assets.get_asset_service] = lambda: service
    return TestClient(app)


def test_upload_image_uses_principal_and_does_not_expose_storage_coordinates(
    fake_business_intent_catalog_runtime,
):
    service = RecordingAssetService()
    client = _client(service, fake_business_intent_catalog_runtime)

    response = client.post(
        "/api/assets/images",
        data={"session_id": "session-1"},
        files={"file": ("report.png", b"source-image", "image/png")},
    )

    assert response.status_code == 202
    assert service.uploads[0][0] == b"source-image"
    assert service.uploads[0][1]["principal"].tenant_id == "tenant-1"
    assert service.uploads[0][1]["claimed_media_type"] == "image/png"
    assert response.json()["asset_id"] == "asset-1"
    assert "object_key" not in response.json()
    assert "sha256" not in response.json()


def test_upload_image_maps_validation_code_to_stable_http_status(
    fake_business_intent_catalog_runtime,
):
    service = RecordingAssetService()
    service.upload_error = AssetValidationError(
        code="unsupported_image_type",
        message="unsupported",
    )
    client = _client(service, fake_business_intent_catalog_runtime)

    response = client.post(
        "/api/assets/images",
        files={"file": ("report.gif", b"GIF89a", "image/gif")},
    )

    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_image_type"


def test_asset_routes_stop_before_service_when_permission_is_missing(
    fake_business_intent_catalog_runtime,
):
    service = RecordingAssetService()
    client = _client(
        service,
        fake_business_intent_catalog_runtime,
        scopes=("asset:read",),
    )

    response = client.post(
        "/api/assets/images",
        files={"file": ("report.png", b"source-image", "image/png")},
    )

    assert response.status_code == 403
    assert service.uploads == []


def test_get_asset_is_tenant_scoped_and_maps_not_found(
    fake_business_intent_catalog_runtime,
):
    service = RecordingAssetService()
    service.get_error = AssetNotFoundError("missing")
    client = _client(service, fake_business_intent_catalog_runtime)

    response = client.get("/api/assets/asset-missing")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "asset_not_found"


def test_delete_asset_is_idempotent_at_http_boundary(
    fake_business_intent_catalog_runtime,
):
    service = RecordingAssetService()
    client = _client(service, fake_business_intent_catalog_runtime)

    response = client.delete("/api/assets/asset-1")

    assert response.status_code == 204
    assert service.deleted == [("asset-1", "tenant-1")]


def test_get_asset_analysis_returns_structured_result_without_raw_provider_payload(
    fake_business_intent_catalog_runtime,
):
    service = RecordingAssetService()
    analysis_record = SimpleNamespace(
        analysis_id="analysis-1",
        asset_id="asset-1",
        tenant_id="tenant-1",
        schema_version="1.0",
        provider="openai_compatible",
        model="vision-model",
        result_json=VisionAnalysis(
            document_type="chart",
            summary="营业收入同比增长。",
            confidence=0.9,
        ).model_dump(mode="json"),
        created_at=datetime.now(UTC),
    )
    repository = SimpleNamespace(get_latest=lambda **_: analysis_record)
    app = create_app(business_intent_catalog_runtime=fake_business_intent_catalog_runtime)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        tenant_id="tenant-1",
        user_id="user-1",
        scopes=("asset:read",),
    )
    app.dependency_overrides[assets.get_asset_service] = lambda: service
    app.dependency_overrides[assets.get_vision_analysis_repository] = lambda: repository
    client = TestClient(app)

    response = client.get("/api/assets/asset-1/analysis")

    assert response.status_code == 200
    assert response.json()["analysis"]["summary"] == "营业收入同比增长。"
    assert "result_json" not in response.json()
