from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LANGFUSE_LOCAL_ALLOWED_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "host.docker.internal", "langfuse-web"}
)


class Settings(BaseSettings):
    service_name: str = "bs-agent-service"
    app_env: Literal["local", "test", "production"] = "local"
    recover_orphaned_runs_on_startup: bool = False
    prewarm_tool_catalog_on_startup: bool = False
    auth_provider: Literal["mock", "jwt"] = "mock"
    rag_base_url: str = "http://localhost:8001"
    rag_timeout_seconds: float = 30.0
    rag_provider_revision: str = "local-dev"
    rag_config_revision: str = "default"
    database_url: str = "sqlite:///./bs_agent.db"
    redis_url: str = "redis://localhost:6379/0"
    memory_store_postgres_url: str = (
        "postgresql://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent"
    )
    experience_memory_recall_limit: int = Field(default=8, ge=1, le=50)
    graph_checkpoint_postgres_url: str | None = None
    graph_recursion_limit: int = Field(default=128, ge=8, le=256)
    admin_base_url: str = "http://localhost:8002"
    skill_registry_base_url: str = "http://localhost:8010"
    intent_catalog_refresh_seconds: int = Field(default=30, ge=5, le=3600)
    market_data_provider_refresh_seconds: int = Field(default=30, ge=5, le=3600)
    skill_definition_cache_ttl_seconds: int = 300
    mcp_server_url: str = ""
    mcp_server_name: str = "agent-mcp"
    mcp_timeout_seconds: float = 30.0
    mcp_server_revision: str = "local-dev"
    mcp_config_revision: str = "default"
    tool_search_top_k: int = Field(default=5, ge=1, le=8)
    tool_search_candidate_k: int = Field(default=50, ge=5, le=128)
    tool_search_max_calls: int = Field(default=4, ge=1, le=16)
    tool_search_max_disclosed_tools: int = Field(default=16, ge=1, le=64)
    planner_max_iterations: int = Field(default=10, ge=1, le=20)
    llm_provider: Literal["deepseek"] = "deepseek"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    llm_timeout_seconds: float = 30.0
    web_search_enabled: bool = True
    web_search_provider: Literal["deepseek"] = "deepseek"
    web_search_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    rbac_backend: Literal["mock", "principal"] = "mock"
    mock_rbac_approver_user_ids: list[str] = ["approver_1"]
    mock_rbac_admin_user_ids: list[str] = ["admin_1"]
    approval_resume_claim_timeout_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
    )

    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_jwks_url: str | None = None
    jwt_public_key: str | None = None
    jwt_allowed_algorithms: list[str] = ["RS256"]
    jwt_jwks_cache_ttl_seconds: int = Field(default=300, ge=30, le=3600)

    max_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    max_image_pixels: int = Field(default=20_000_000, ge=1)
    max_message_attachments: int = Field(default=4, ge=1, le=16)
    image_allowed_media_types: tuple[str, ...] = (
        "image/jpeg",
        "image/png",
        "image/webp",
    )
    asset_retention_days: int = Field(default=7, ge=1)
    asset_cleanup_batch_size: int = Field(default=500, ge=1, le=10_000)
    asset_cleanup_interval_seconds: int = Field(default=3600, ge=60, le=86_400)
    asset_store_backend: Literal["local", "s3"] = "local"
    asset_local_root: str = "./data/assets"
    asset_s3_endpoint_url: str | None = None
    asset_s3_bucket: str | None = None
    asset_s3_region: str | None = None
    asset_s3_server_side_encryption: Literal["AES256", "aws:kms"] = "AES256"
    asset_s3_kms_key_id: str | None = None
    image_analysis_dispatcher_backend: Literal["noop", "celery"] = "noop"
    celery_broker_url: str = "redis://localhost:6379/1"
    vision_provider: Literal["none", "openai_compatible"] = "none"
    vision_api_key: str = ""
    vision_base_url: str = ""
    vision_model: str = ""
    vision_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    vision_max_output_tokens: int = Field(default=2048, ge=256, le=8192)
    ocr_provider: Literal["none", "paddleocr_http"] = "none"
    ocr_base_url: str = ""
    ocr_api_key: str = ""
    ocr_provider_version: str = ""
    ocr_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    image_analysis_lock_ttl_seconds: int = Field(default=300, ge=30, le=1800)
    image_analysis_lock_wait_seconds: float = Field(default=1.5, ge=0, le=30)
    provider_circuit_fail_max: int = Field(default=5, ge=1, le=100)
    provider_circuit_reset_seconds: int = Field(default=60, ge=1, le=3600)
    provider_max_concurrency: int = Field(default=4, ge=1, le=64)
    provider_concurrency_wait_seconds: float = Field(default=0.5, ge=0, le=30)
    rate_limit_backend: Literal["none", "redis"] = "none"
    agent_rate_limit_per_minute: int = Field(default=20, ge=1, le=10_000)
    image_upload_rate_limit_per_minute: int = Field(default=10, ge=1, le=10_000)
    rate_limit_fail_closed: bool = True
    log_format: Literal["console", "json"] = "console"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    otel_enabled: bool = False
    otel_exporter_otlp_traces_endpoint: str | None = None
    otel_trace_sample_ratio: float = Field(default=0.1, ge=0, le=1)
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "http://localhost:3000"
    langfuse_sample_rate: float = Field(default=1.0, ge=0, le=1)
    langfuse_content_mode: Literal["metadata_only", "local_content", "local_debug"] = (
        "local_content"
    )
    langfuse_capture_reasoning: bool = False
    trace_backend: Literal["none", "memory", "database"] = "memory"
    trace_success_sample_ratio: float = Field(default=0.1, ge=0, le=1)
    trace_detailed_sample_ratio: float = Field(default=0.01, ge=0, le=1)
    trace_retention_days: int = Field(default=30, ge=1, le=365)
    trace_cleanup_batch_size: int = Field(default=1000, ge=1, le=10_000)
    trace_max_events_per_run: int = Field(default=128, ge=8, le=2048)
    trace_max_artifacts_per_run: int = Field(default=16, ge=0, le=256)
    trace_max_artifact_bytes: int = Field(
        default=32 * 1024,
        ge=1024,
        le=1024 * 1024,
    )

    @model_validator(mode="after")
    def validate_production_configuration(self):
        if self.trace_detailed_sample_ratio > self.trace_success_sample_ratio:
            raise ValueError(
                "trace_detailed_sample_ratio 不能大于 "
                "trace_success_sample_ratio"
            )
        if self.otel_enabled and not _has_text(
            self.otel_exporter_otlp_traces_endpoint
        ):
            raise ValueError(
                "启用 OpenTelemetry 时必须配置 "
                "otel_exporter_otlp_traces_endpoint"
            )
        if self.langfuse_enabled:
            missing = [
                field_name
                for field_name, value in (
                    ("langfuse_public_key", self.langfuse_public_key),
                    ("langfuse_secret_key", self.langfuse_secret_key),
                )
                if not _has_text(value)
            ]
            if missing:
                raise ValueError("启用 Langfuse 时必须配置 " + ", ".join(missing))
            parsed_url = urlparse(self.langfuse_base_url)
            if (
                parsed_url.scheme not in {"http", "https"}
                or parsed_url.hostname not in _LANGFUSE_LOCAL_ALLOWED_HOSTS
                or parsed_url.username is not None
                or parsed_url.password is not None
            ):
                raise ValueError("启用 Langfuse 时必须使用本地 Langfuse 地址")
        if self.langfuse_capture_reasoning and self.langfuse_content_mode != "local_debug":
            raise ValueError(
                "langfuse_capture_reasoning=true 仅允许用于 local_debug 模式"
            )
        if not _is_postgres_url(self.memory_store_postgres_url):
            raise ValueError("memory_store_postgres_url 必须是 PostgreSQL URL")
        if self.tool_search_top_k > self.tool_search_candidate_k:
            raise ValueError(
                "tool_search_top_k 不能大于 tool_search_candidate_k"
            )
        if self.tool_search_top_k > self.tool_search_max_disclosed_tools:
            raise ValueError(
                "tool_search_top_k 不能大于 tool_search_max_disclosed_tools"
            )
        if self.app_env != "production":
            return self

        invalid: list[str] = []
        if self.auth_provider != "jwt":
            invalid.append("auth_provider")
        if self.rbac_backend != "principal":
            invalid.append("rbac_backend")
        if not self.database_url.strip().lower().startswith("postgresql"):
            invalid.append("database_url")
        if self.rate_limit_backend != "redis":
            invalid.append("rate_limit_backend")
        if self.log_format != "json":
            invalid.append("log_format")
        if self.trace_backend != "database":
            invalid.append("trace_backend")
        if self.recover_orphaned_runs_on_startup:
            invalid.append("recover_orphaned_runs_on_startup")
        if not self.otel_enabled:
            invalid.append("otel_enabled")
        if not _has_text(self.rag_base_url):
            invalid.append("rag_base_url")
        if not _has_text(self.skill_registry_base_url):
            invalid.append("skill_registry_base_url")
        if not _has_text(self.admin_base_url):
            invalid.append("admin_base_url")
        if _is_development_revision(self.rag_provider_revision):
            invalid.append("rag_provider_revision")
        if _is_development_revision(self.rag_config_revision):
            invalid.append("rag_config_revision")
        if _has_text(self.mcp_server_url):
            if _is_development_revision(self.mcp_server_revision):
                invalid.append("mcp_server_revision")
            if _is_development_revision(self.mcp_config_revision):
                invalid.append("mcp_config_revision")
            if not _is_production_mcp_url(self.mcp_server_url):
                invalid.append("mcp_server_url")
        if not self.redis_url.strip():
            invalid.append("redis_url")
        if not _is_postgres_url(self.graph_checkpoint_postgres_url):
            invalid.append("graph_checkpoint_postgres_url")
        if self.asset_store_backend != "s3":
            invalid.append("asset_store_backend")
        if not _has_text(self.asset_s3_bucket):
            invalid.append("asset_s3_bucket")
        if not _has_text(self.asset_s3_region):
            invalid.append("asset_s3_region")
        if not _has_text(self.jwt_issuer):
            invalid.append("jwt_issuer")
        if not _has_text(self.jwt_audience):
            invalid.append("jwt_audience")
        if not (_has_text(self.jwt_jwks_url) or _has_text(self.jwt_public_key)):
            invalid.extend(["jwt_jwks_url", "jwt_public_key"])
        if not self.deepseek_api_key.strip():
            invalid.append("deepseek_api_key")
        if self.web_search_enabled and self.web_search_provider != "deepseek":
            invalid.append("web_search_provider")
        if self.image_analysis_dispatcher_backend != "celery":
            invalid.append("image_analysis_dispatcher_backend")
        if not self.celery_broker_url.strip():
            invalid.append("celery_broker_url")
        if self.vision_provider != "openai_compatible":
            invalid.append("vision_provider")
        if not self.vision_api_key.strip():
            invalid.append("vision_api_key")
        if not self.vision_base_url.strip():
            invalid.append("vision_base_url")
        if not self.vision_model.strip():
            invalid.append("vision_model")
        if self.ocr_provider != "paddleocr_http":
            invalid.append("ocr_provider")
        if not self.ocr_base_url.strip():
            invalid.append("ocr_base_url")
        if not self.ocr_provider_version.strip():
            invalid.append("ocr_provider_version")
        if (
            self.asset_s3_server_side_encryption == "aws:kms"
            and not _has_text(self.asset_s3_kms_key_id)
        ):
            invalid.append("asset_s3_kms_key_id")

        if invalid:
            fields = ", ".join(dict.fromkeys(invalid))
            raise ValueError(f"生产环境配置不完整或不安全: {fields}")
        return self

    model_config = SettingsConfigDict(env_prefix="BS_AGENT_")


def _has_text(value: str | None) -> bool:
    return value is not None and bool(value.strip())


def _is_postgres_url(value: str | None) -> bool:
    if not _has_text(value):
        return False
    normalized = value.strip().lower()
    return normalized.startswith(("postgresql://", "postgres://"))


def _is_development_revision(value: str | None) -> bool:
    return not _has_text(value) or value.strip().lower() in {"default", "local-dev"}


def _is_production_mcp_url(value: str | None) -> bool:
    if not _has_text(value):
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        and bool(parsed.path.strip("/"))
    )


settings = Settings()
