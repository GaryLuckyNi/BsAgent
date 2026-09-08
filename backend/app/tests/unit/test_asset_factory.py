from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.assets.factory import build_asset_service, build_object_store
from app.assets.local_store import LocalObjectStore
from app.assets.s3_store import S3ObjectStore
from app.assets.service import NoopImageAnalysisDispatcher
from app.core.config import Settings
from app.storage.models import Base
from app.vision.dispatcher import CeleryImageAnalysisDispatcher
from app.vision.factory import build_image_analysis_dispatcher


def _production_settings(**overrides):
    values = {
        "app_env": "production",
        "auth_provider": "jwt",
        "rbac_backend": "principal",
        "database_url": "postgresql+psycopg://agent@postgres/agent",
        "memory_store_postgres_url": "postgresql://agent@postgres/agent",
        "graph_checkpoint_postgres_url": "postgresql://agent@postgres/agent",
        "asset_store_backend": "s3",
        "asset_s3_bucket": "agent-private-assets",
        "asset_s3_region": "cn-north-1",
        "jwt_issuer": "https://identity.example.test",
        "jwt_audience": "bs-agent",
        "jwt_public_key": "test-public-key",
        "deepseek_api_key": "test-api-key",
        "image_analysis_dispatcher_backend": "celery",
        "vision_provider": "openai_compatible",
        "vision_api_key": "test-vision-api-key",
        "vision_base_url": "https://vision.example.test/v1",
        "vision_model": "vision-model",
        "ocr_provider": "paddleocr_http",
        "ocr_base_url": "http://paddleocr:8000",
        "ocr_provider_version": "paddleocr-v3",
        "rag_provider_revision": "rag-service-2026.07",
        "rag_config_revision": "research-v1",
        "mcp_server_revision": "research-tools-2026.07",
        "mcp_config_revision": "production-v1",
        "mcp_server_url": "http://mcp-server:8011/mcp",
        "tool_search_local_files_only": True,
        "rate_limit_backend": "redis",
        "log_format": "json",
        "trace_backend": "database",
        "otel_enabled": True,
        "otel_exporter_otlp_traces_endpoint": "http://otel-collector:4318/v1/traces",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_build_object_store_uses_local_backend_for_local_development(tmp_path):
    config = Settings(
        _env_file=None,
        app_env="local",
        asset_store_backend="local",
        asset_local_root=str(tmp_path),
    )

    store = build_object_store(config=config)

    assert isinstance(store, LocalObjectStore)


def test_build_object_store_configures_s3_timeout_retry_and_encryption():
    config = _production_settings(asset_s3_endpoint_url="https://s3.example.test")
    client = Mock()

    with patch("app.assets.factory.boto3.client", return_value=client) as create_client:
        store = build_object_store(config=config)

    assert isinstance(store, S3ObjectStore)
    call = create_client.call_args
    assert call.args == ("s3",)
    assert call.kwargs["endpoint_url"] == "https://s3.example.test"
    assert call.kwargs["region_name"] == "cn-north-1"
    client_config = call.kwargs["config"]
    assert client_config.connect_timeout == 3
    assert client_config.read_timeout == 15
    assert client_config.retries["mode"] == "adaptive"
    assert client_config.signature_version == "s3v4"


def test_build_asset_service_rejects_noop_dispatcher_in_production():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="NoopImageAnalysisDispatcher"):
            build_asset_service(
                session=session,
                dispatcher=NoopImageAnalysisDispatcher(),
                config=_production_settings(),
                object_store=Mock(),
            )


def test_build_image_dispatcher_uses_celery_in_production():
    celery_app = Mock()

    dispatcher = build_image_analysis_dispatcher(
        config=_production_settings(),
        celery_app=celery_app,
    )

    assert isinstance(dispatcher, CeleryImageAnalysisDispatcher)
