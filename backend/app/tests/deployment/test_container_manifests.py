from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _compose() -> dict:
    path = PROJECT_ROOT / "compose.production.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_production_compose_contains_required_runtime_services():
    compose = _compose()

    assert {
        "postgres",
        "redis",
        "minio",
        "minio-init",
        "ocr",
        "migrate",
        "release-gate",
        "api",
        "worker",
        "maintenance-worker",
        "scheduler",
        "frontend",
    }.issubset(compose["services"])


def test_api_waits_for_migrations_and_healthy_stateful_dependencies():
    api = _compose()["services"]["api"]

    assert api["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert api["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert api["depends_on"]["redis"]["condition"] == "service_healthy"
    assert api["depends_on"]["minio"]["condition"] == "service_healthy"
    assert api["healthcheck"]["test"][-1] == "http://127.0.0.1:8000/health/ready"


def test_compose_forces_postgres_saver_and_externalized_secrets():
    compose_text = (PROJECT_ROOT / "compose.production.yml").read_text(encoding="utf-8")
    api_environment = _compose()["services"]["api"]["environment"]

    assert api_environment["BS_AGENT_APP_ENV"] == "production"
    assert api_environment["BS_AGENT_GRAPH_CHECKPOINTER_BACKEND"] == "postgres"
    assert api_environment["BS_AGENT_GRAPH_CHECKPOINT_POSTGRES_URL"].startswith(
        "postgresql://"
    )
    assert "postgresql+psycopg://" in api_environment["BS_AGENT_DATABASE_URL"]
    assert "BS_AGENT_DEEPSEEK_API_KEY: test" not in compose_text
    assert "DEEPSEEK_API_KEY:?" in compose_text
    assert "VISION_API_KEY:?" in compose_text
    assert "OCR_API_KEY:?" in compose_text


def test_production_compose_configures_database_trace_capture():
    environment = _compose()["services"]["api"]["environment"]

    assert environment["BS_AGENT_TRACE_BACKEND"] == "database"
    assert environment["BS_AGENT_TRACE_SUCCESS_SAMPLE_RATIO"] == "${TRACE_SUCCESS_SAMPLE_RATIO:-0.1}"
    assert environment["BS_AGENT_TRACE_DETAILED_SAMPLE_RATIO"] == "${TRACE_DETAILED_SAMPLE_RATIO:-0.01}"
    assert environment["BS_AGENT_TRACE_RETENTION_DAYS"] == "${TRACE_RETENTION_DAYS:-30}"
    assert environment["BS_AGENT_TRACE_CLEANUP_BATCH_SIZE"] == "${TRACE_CLEANUP_BATCH_SIZE:-1000}"
    assert environment["BS_AGENT_TRACE_MAX_EVENTS_PER_RUN"] == "${TRACE_MAX_EVENTS_PER_RUN:-128}"
    assert environment["BS_AGENT_TRACE_MAX_ARTIFACTS_PER_RUN"] == "${TRACE_MAX_ARTIFACTS_PER_RUN:-16}"
    assert environment["BS_AGENT_TRACE_MAX_ARTIFACT_BYTES"] == "${TRACE_MAX_ARTIFACT_BYTES:-32768}"
    assert environment["BS_AGENT_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == (
        "http://otel-collector:4318/v1/traces"
    )


def test_worker_uses_same_backend_image_and_dedicated_queue():
    services = _compose()["services"]
    api = services["api"]
    worker = services["worker"]

    assert worker["build"] == api["build"]
    assert worker["command"][-1] == "image-analysis"
    assert worker["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert worker["depends_on"]["release-gate"]["condition"] == (
        "service_completed_successfully"
    )

    maintenance = services["maintenance-worker"]
    assert maintenance["build"] == api["build"]
    assert maintenance["command"][-1] == "maintenance"
    assert maintenance["environment"]["BS_AGENT_TRACE_RETENTION_DAYS"] == (
        "${TRACE_RETENTION_DAYS:-30}"
    )
    assert maintenance["environment"]["BS_AGENT_TRACE_CLEANUP_BATCH_SIZE"] == (
        "${TRACE_CLEANUP_BATCH_SIZE:-1000}"
    )
    assert maintenance["depends_on"]["release-gate"]["condition"] == (
        "service_completed_successfully"
    )

    scheduler = services["scheduler"]
    assert scheduler["build"] == api["build"]
    assert "beat" in scheduler["command"]
    assert scheduler["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert scheduler["depends_on"]["release-gate"]["condition"] == (
        "service_completed_successfully"
    )


def test_release_gate_blocks_runtime_services_until_database_is_drained():
    services = _compose()["services"]
    gate = services["release-gate"]

    assert gate["command"] == ["python", "-m", "app.operations.production_gate"]
    assert gate["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert gate["restart"] == "no"
    for service_name in ("api", "worker", "maintenance-worker", "scheduler"):
        assert services[service_name]["depends_on"]["release-gate"]["condition"] == (
            "service_completed_successfully"
        )


def test_container_builds_run_as_non_root_and_frontend_uses_nginx():
    backend_dockerfile = (PROJECT_ROOT / "backend" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    frontend_dockerfile = (PROJECT_ROOT / "frontend" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    nginx_config = (PROJECT_ROOT / "frontend" / "nginx.conf").read_text(
        encoding="utf-8"
    )

    assert "USER bsagent" in backend_dockerfile
    assert "USER nginx" in frontend_dockerfile
    assert "FROM nginx:" in frontend_dockerfile
    assert "proxy_pass http://api:8000" in nginx_config
    assert "location /health" in nginx_config


def test_python_images_install_reproducible_lock_files():
    backend_dockerfile = (PROJECT_ROOT / "backend" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    ocr_dockerfile = (PROJECT_ROOT / "ocr-service" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "backend/requirements.lock" in backend_dockerfile
    assert "pip install --requirement requirements.lock" in backend_dockerfile
    assert "pip check" in backend_dockerfile
    assert "ocr-service/requirements.lock" in ocr_dockerfile
    assert "pip install --requirement requirements.lock" in ocr_dockerfile
    assert "pip check" in ocr_dockerfile


def test_frontend_oidc_configuration_is_injected_at_container_startup():
    compose_text = (PROJECT_ROOT / "compose.production.yml").read_text(
        encoding="utf-8"
    )
    dockerfile = (PROJECT_ROOT / "frontend" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    nginx_config = (PROJECT_ROOT / "frontend" / "nginx.conf").read_text(
        encoding="utf-8"
    )
    index_html = (PROJECT_ROOT / "frontend" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "OIDC_AUTHORITY:?" in compose_text
    assert "OIDC_CLIENT_ID:?" in compose_text
    assert "VITE_AGENT_ACCESS_TOKEN" not in compose_text
    assert "start-nginx.sh" in dockerfile
    assert "location = /runtime-config.js" in nginx_config
    assert "${OIDC_CONNECT_SRC}" in nginx_config
    assert '<script src="/runtime-config.js"></script>' in index_html


def test_migration_script_runs_business_and_checkpoint_schema_setup():
    script = (PROJECT_ROOT / "deploy" / "run-migrations.sh").read_text(
        encoding="utf-8"
    )

    assert "alembic upgrade head" in script
    assert "python -m app.operations.checkpoint_setup" in script


def test_production_environment_template_contains_no_live_credentials():
    template = (PROJECT_ROOT / "deploy" / "production.env.example").read_text(
        encoding="utf-8"
    )

    assert "POSTGRES_PASSWORD_URLENCODED=" in template
    assert "OCR_API_KEY=" in template
    assert "sk-" not in template
    assert "replace-with" in template


def test_otel_collector_exports_to_external_backend_instead_of_debug_stdout():
    collector = (PROJECT_ROOT / "deploy" / "otel-collector.yaml").read_text(
        encoding="utf-8"
    )
    compose_text = (PROJECT_ROOT / "compose.production.yml").read_text(
        encoding="utf-8"
    )

    assert "otlphttp:" in collector
    assert "OBSERVABILITY_OTLP_ENDPOINT" in collector
    assert "exporters: [otlphttp]" in collector
    assert "exporters: [debug]" not in collector
    assert "OBSERVABILITY_OTLP_ENDPOINT:?" in compose_text


def test_ci_enforces_backend_frontend_and_postgres_checkpoint_gates():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "python-version: \"3.11\"" in workflow
    assert "backend/requirements.lock" in workflow
    assert "BS_AGENT_TEST_POSTGRES_CHECKPOINT_URL" in workflow
    assert "python -m pytest -q" in workflow
    assert "python -m ruff check ." in workflow
    assert "npm ci" in workflow
    assert "npm test -- --run" in workflow
    assert "npm run build" in workflow
    assert "npm audit --omit=dev" in workflow
    assert "docker compose" in workflow
    assert "config --quiet" in workflow


def test_production_smoke_covers_image_analysis_and_streaming_agent():
    script = (PROJECT_ROOT / "deploy" / "smoke-production.sh").read_text(
        encoding="utf-8"
    )

    assert "/health/ready" in script
    assert "/api/assets/images" in script
    assert "/analysis" in script
    assert "ocr_vision_hybrid" in script
    assert "/messages/stream" in script
    assert "event: final" in script
    assert "ACCESS_TOKEN" in script
    assert "TEST_IMAGE" in script
