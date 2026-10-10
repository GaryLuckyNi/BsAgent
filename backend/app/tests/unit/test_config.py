import pytest
from app.core.config import Settings
from pydantic import ValidationError


def test_context_compaction_defaults_are_conservative_and_disabled():
    settings = Settings(_env_file=None)

    assert settings.context_compaction_enabled is False
    assert settings.llm_context_window_tokens == 131_072
    assert settings.context_compaction_target_ratio == 0.50
    assert settings.context_compaction_soft_ratio == 0.70
    assert settings.context_compaction_hard_ratio == 0.85
    assert settings.context_compaction_keep_turns == 3
    assert settings.context_compaction_min_keep_turns == 1
    assert settings.context_summary_target_tokens == 2_000
    assert settings.context_summary_max_tokens == 3_000
    assert settings.context_non_history_reserve_tokens == 24_000
    assert settings.context_compaction_next_user_reserve_tokens == 2_048


@pytest.mark.parametrize(
    "overrides",
    [
        {"context_compaction_target_ratio": 0.70},
        {"context_compaction_soft_ratio": 0.85},
        {"context_compaction_target_ratio": 0.90},
        {"context_compaction_min_keep_turns": 4},
        {"context_summary_target_tokens": 3_001},
    ],
)
def test_context_compaction_rejects_invalid_cross_field_settings(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_approval_resume_claim_timeout_defaults_to_five_minutes():
    assert Settings(_env_file=None).approval_resume_claim_timeout_seconds == 300


def test_intent_catalog_refresh_defaults_to_thirty_seconds():
    assert Settings(_env_file=None).intent_catalog_refresh_seconds == 30


def test_market_data_provider_refresh_defaults_to_thirty_seconds():
    assert Settings(_env_file=None).market_data_provider_refresh_seconds == 30


def test_orphaned_run_recovery_requires_explicit_local_opt_in():
    assert Settings(_env_file=None).recover_orphaned_runs_on_startup is False
    assert Settings(_env_file=None).prewarm_tool_catalog_on_startup is False


@pytest.mark.parametrize("value", [4, 3601])
def test_intent_catalog_refresh_enforces_bounds(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, intent_catalog_refresh_seconds=value)


@pytest.mark.parametrize("value", [29, 3601])
def test_approval_resume_claim_timeout_enforces_bounds(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, approval_resume_claim_timeout_seconds=value)


def _production_settings(**overrides):
    values = {
        "app_env": "production",
        "auth_provider": "jwt",
        "rbac_backend": "principal",
        "database_url": "postgresql+psycopg://agent@postgres/agent",
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
        "mcp_server_url": "https://api.tushare.pro/mcp/token=test-token",
        "memory_store_postgres_url": "postgresql://agent@postgres/agent",
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
        ({"image_analysis_dispatcher_backend": "noop"}, "image_analysis_dispatcher_backend"),
        ({"rate_limit_backend": "none"}, "rate_limit_backend"),
        ({"log_format": "console"}, "log_format"),
        ({"trace_backend": "memory"}, "trace_backend"),
        ({"otel_enabled": False}, "otel_enabled"),
        ({"recover_orphaned_runs_on_startup": True}, "recover_orphaned_runs_on_startup"),
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
        ({"mcp_server_url": "not-a-url"}, "mcp_server_url"),
        ({"mcp_server_url": "http://127.0.0.1:8011/mcp"}, "mcp_server_url"),
        ({"memory_store_postgres_url": None}, "memory_store_postgres_url"),
        ({"memory_store_postgres_url": "sqlite:///agent.db"}, "memory_store_postgres_url"),
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
    assert settings.graph_checkpoint_postgres_url == "postgresql://agent@postgres/agent"
    assert settings.rate_limit_backend == "redis"
    assert settings.log_format == "json"


def test_production_allows_generic_mcp_to_be_unconfigured():
    settings = _production_settings(
        mcp_server_url="",
        mcp_server_revision="local-dev",
        mcp_config_revision="default",
    )

    assert settings.mcp_server_url == ""


def test_settings_do_not_expose_a_separate_short_term_memory_backend():
    runtime = Settings(_env_file=None)

    assert not hasattr(runtime, "short_term_memory_backend")
    assert not hasattr(runtime, "short_term_memory_ttl_seconds")
    assert not hasattr(runtime, "short_term_memory_max_events")


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
    assert settings.admin_base_url == "http://localhost:8002"
    assert settings.market_data_provider_refresh_seconds == 30
    assert settings.mcp_server_name == "agent-mcp"
    assert settings.mcp_server_url == ""
    assert settings.trace_cleanup_batch_size == 1000
    assert settings.trace_max_events_per_run == 128
    assert settings.trace_max_artifacts_per_run == 16
    assert settings.trace_max_artifact_bytes == 32 * 1024
    assert settings.memory_store_postgres_url == (
        "postgresql://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent"
    )


def test_tool_search_settings_have_safe_defaults():
    settings = Settings(_env_file=None)

    assert settings.tool_search_top_k == 5
    assert settings.tool_search_candidate_k == 50
    assert settings.tool_search_max_calls == 4
    assert settings.tool_search_max_disclosed_tools == 16
    assert not any(
        name.startswith("tool_search_embedding")
        for name in type(settings).model_fields
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tool_search_top_k", 0),
        ("tool_search_top_k", 9),
        ("tool_search_candidate_k", 4),
        ("tool_search_candidate_k", 129),
        ("tool_search_max_calls", 0),
        ("tool_search_max_calls", 17),
        ("tool_search_max_disclosed_tools", 0),
        ("tool_search_max_disclosed_tools", 65),
    ],
)
def test_tool_search_settings_enforce_bounds(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"tool_search_top_k": 6, "tool_search_candidate_k": 5},
        {"tool_search_top_k": 5, "tool_search_max_disclosed_tools": 4},
    ],
)
def test_tool_search_top_k_cannot_exceed_disclosure_limits(overrides):
    with pytest.raises(ValidationError, match="tool_search_top_k"):
        Settings(_env_file=None, **overrides)


@pytest.mark.parametrize("app_env", ["local", "test", "production"])
def test_memory_store_accepts_postgres_url_in_every_environment(app_env):
    if app_env == "production":
        runtime = _production_settings()
    else:
        runtime = Settings(
            _env_file=None,
            app_env=app_env,
            memory_store_postgres_url="postgresql://agent@postgres/agent",
        )

    assert runtime.memory_store_postgres_url == "postgresql://agent@postgres/agent"


@pytest.mark.parametrize("app_env", ["local", "test"])
@pytest.mark.parametrize("memory_store_postgres_url", ["", "sqlite:///agent.db"])
def test_local_and_test_reject_missing_or_non_postgres_memory_store_url(
    app_env,
    memory_store_postgres_url,
):
    with pytest.raises(ValidationError, match="memory_store_postgres_url"):
        Settings(
            _env_file=None,
            app_env=app_env,
            memory_store_postgres_url=memory_store_postgres_url,
        )


def test_settings_remove_custom_long_term_store_configuration():
    assert all(
        not field.startswith("experience" + "_store")
        for field in Settings.model_fields
    )


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trace_retention_days", 0),
        ("trace_retention_days", 366),
        ("trace_cleanup_batch_size", 0),
        ("trace_cleanup_batch_size", 10_001),
    ],
)
def test_trace_retention_settings_enforce_bounds(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
