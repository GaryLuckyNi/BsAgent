import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _production_settings(**overrides):
    values = {
        "app_env": "production",
        "auth_provider": "jwt",
        "rbac_backend": "principal",
        "database_url": "postgresql+psycopg://agent@postgres/agent",
        "short_term_memory_backend": "redis",
        "graph_checkpointer_backend": "postgres",
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
        "rate_limit_backend": "redis",
        "log_format": "json",
        "trace_backend": "database",
        "otel_enabled": True,
        "otel_exporter_otlp_traces_endpoint": "http://otel-collector:4318/v1/traces",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("overrides", "expected_field"),
    [
        ({"auth_provider": "mock"}, "auth_provider"),
        ({"rbac_backend": "mock"}, "rbac_backend"),
        ({"asset_store_backend": "local"}, "asset_store_backend"),
        ({"database_url": "sqlite:///agent.db"}, "database_url"),
        ({"graph_checkpointer_backend": "sqlite"}, "graph_checkpointer_backend"),
        ({"short_term_memory_backend": "none"}, "short_term_memory_backend"),
        ({"image_analysis_dispatcher_backend": "noop"}, "image_analysis_dispatcher_backend"),
        ({"rate_limit_backend": "none"}, "rate_limit_backend"),
        ({"log_format": "console"}, "log_format"),
        ({"trace_backend": "memory"}, "trace_backend"),
        ({"otel_enabled": False}, "otel_enabled"),
        ({"use_mock_rag": True}, "use_mock_rag"),
    ],
)
def test_production_rejects_development_backends(overrides, expected_field):
    with pytest.raises(ValidationError) as exc_info:
        _production_settings(**overrides)

    assert expected_field in str(exc_info.value)


@pytest.mark.parametrize(
    ("overrides", "expected_field"),
    [
        ({"jwt_issuer": ""}, "jwt_issuer"),
        ({"jwt_audience": ""}, "jwt_audience"),
        ({"jwt_public_key": None, "jwt_jwks_url": None}, "jwt_jwks_url"),
        ({"asset_s3_bucket": ""}, "asset_s3_bucket"),
        ({"asset_s3_region": ""}, "asset_s3_region"),
        ({"graph_checkpoint_postgres_url": None}, "graph_checkpoint_postgres_url"),
        (
            {"graph_checkpoint_postgres_url": "postgresql+psycopg://agent@postgres/agent"},
            "graph_checkpoint_postgres_url",
        ),
        ({"deepseek_api_key": ""}, "deepseek_api_key"),
        ({"vision_provider": "none"}, "vision_provider"),
        ({"vision_api_key": ""}, "vision_api_key"),
        ({"vision_model": ""}, "vision_model"),
        ({"ocr_provider": "none"}, "ocr_provider"),
        ({"ocr_base_url": ""}, "ocr_base_url"),
        ({"ocr_provider_version": ""}, "ocr_provider_version"),
        ({"rag_base_url": ""}, "rag_base_url"),
        ({"skill_registry_base_url": ""}, "skill_registry_base_url"),
        ({"rag_provider_revision": "local-dev"}, "rag_provider_revision"),
        ({"mcp_server_revision": "local-dev"}, "mcp_server_revision"),
    ],
)
def test_production_requires_external_service_configuration(overrides, expected_field):
    with pytest.raises(ValidationError) as exc_info:
        _production_settings(**overrides)

    assert expected_field in str(exc_info.value)


def test_production_accepts_complete_externalized_configuration():
    settings = _production_settings()

    assert settings.app_env == "production"
    assert settings.auth_provider == "jwt"
    assert settings.asset_store_backend == "s3"
    assert settings.graph_checkpointer_backend == "postgres"
    assert settings.rate_limit_backend == "redis"
    assert settings.log_format == "json"


def test_local_defaults_remain_offline_friendly():
    settings = Settings(_env_file=None)

    assert settings.app_env == "local"
    assert settings.auth_provider == "mock"
    assert settings.asset_store_backend == "local"
    assert settings.max_image_bytes == 10 * 1024 * 1024
    assert settings.max_image_pixels == 20_000_000
    assert settings.max_message_attachments == 4
    assert settings.image_analysis_dispatcher_backend == "noop"
    assert settings.vision_provider == "none"
    assert settings.ocr_provider == "none"
    assert settings.rate_limit_backend == "none"
    assert settings.log_format == "console"
    assert settings.trace_backend == "memory"
    assert settings.trace_success_sample_ratio == 0.1
    assert settings.trace_detailed_sample_ratio == 0.01
    assert settings.trace_retention_days == 30
    assert settings.trace_cleanup_batch_size == 1000
    assert settings.trace_max_events_per_run == 128
    assert settings.trace_max_artifacts_per_run == 16
    assert settings.trace_max_artifact_bytes == 32 * 1024


def test_otel_requires_trace_endpoint_when_enabled():
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, otel_enabled=True)

    assert "otel_exporter_otlp_traces_endpoint" in str(exc_info.value)


def test_trace_detailed_ratio_cannot_exceed_success_ratio():
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            trace_success_sample_ratio=0.1,
            trace_detailed_sample_ratio=0.2,
        )

    assert "trace_detailed_sample_ratio" in str(exc_info.value)
