import json
import logging
from io import StringIO

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.operations.observability import (
    RequestContextMiddleware,
    SafeJsonFormatter,
    TelemetryRuntime,
    bind_principal_log_context,
    configure_logging,
    configure_telemetry,
    configure_worker_telemetry,
)
from app.security.principal import Principal


def test_request_context_uses_route_template_and_never_logs_query():
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    @app.get("/items/{item_id}")
    def item(item_id: str):
        bind_principal_log_context(
            Principal(
                tenant_id="tenant-sensitive",
                user_id="user-sensitive",
                scopes=(),
            )
        )
        return {"item_id": item_id}

    logger = logging.getLogger("bs_agent.access")
    previous_level = logger.level
    handler = CaptureHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        response = TestClient(app).get(
            "/items/customer-42?token=query-secret",
            headers={"X-Request-ID": "request-123"},
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "request-123"
    record = next(record for record in records if record.name == "bs_agent.access")
    assert record.route == "/items/{item_id}"
    assert record.request_id == "request-123"
    assert record.status_code == 200
    serialized = str(record.__dict__)
    assert "customer-42" not in serialized
    assert "query-secret" not in serialized
    assert "tenant-sensitive" not in serialized
    assert "user-sensitive" not in serialized


def test_request_context_replaces_invalid_external_request_id():
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/")
    def root():
        return {"ok": True}

    response = TestClient(app).get(
        "/",
        headers={"X-Request-ID": "invalid request id with spaces"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "invalid request id with spaces"
    assert len(response.headers["X-Request-ID"]) == 32


def test_safe_json_formatter_redacts_fields_and_inline_credentials():
    record = logging.LogRecord(
        name="bs_agent.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="provider failed: redis://user:secret@redis.internal:6379/0",
        args=(),
        exc_info=None,
    )
    record.request_id = "request-123"
    record.authorization = "Bearer abcdefghijklmnop"
    formatter = SafeJsonFormatter()

    payload = json.loads(formatter.format(record))

    assert payload["request_id"] == "request-123"
    assert payload["authorization"] == "***"
    serialized = json.dumps(payload)
    assert "secret" not in serialized
    assert "abcdefghijklmnop" not in serialized


def test_created_application_installs_request_context_middleware():
    from app.main import create_app
    from app.operations.health import ReadinessService

    app = create_app(readiness_service=ReadinessService(probes=[]))
    with TestClient(app) as client:
        response = client.get(
            "/health/live",
            headers={"X-Request-ID": "request-app-123"},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "request-app-123"


def test_configure_logging_emits_safe_structured_json():
    stream = StringIO()
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    try:
        configure_logging(log_format="json", level="INFO", stream=stream)
        logging.getLogger("bs_agent.worker").info(
            "failed redis://user:secret@redis.internal:6379/0",
            extra={"authorization": "Bearer abcdefghijklmnop"},
        )
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)

    payload = json.loads(stream.getvalue())
    assert payload["levelname"] == "INFO"
    assert payload["name"] == "bs_agent.worker"
    assert payload["authorization"] == "***"
    assert "secret" not in stream.getvalue()
    assert "abcdefghijklmnop" not in stream.getvalue()


def test_configure_telemetry_is_noop_when_disabled():
    calls = []

    def installer(**kwargs):
        calls.append(kwargs)

    from app.core.config import Settings

    handle = configure_telemetry(
        app=FastAPI(),
        engine=object(),
        config=Settings(_env_file=None, otel_enabled=False),
        installer=installer,
    )

    assert handle is None
    assert calls == []


def test_configure_telemetry_delegates_enabled_runtime_to_installer():
    marker = object()
    captured = {}

    def installer(**kwargs):
        captured.update(kwargs)
        return marker

    from app.core.config import Settings

    app = FastAPI()
    engine = object()
    config = Settings(
        _env_file=None,
        otel_enabled=True,
        otel_exporter_otlp_traces_endpoint="http://collector:4318/v1/traces",
        otel_trace_sample_ratio=0.25,
    )

    handle = configure_telemetry(
        app=app,
        engine=engine,
        config=config,
        installer=installer,
    )

    assert handle is marker
    assert captured == {
        "app": app,
        "engine": engine,
        "service_name": "bs-agent-service",
        "environment": "local",
        "endpoint": "http://collector:4318/v1/traces",
        "sample_ratio": 0.25,
    }


def test_configure_telemetry_allows_exporter_injection_for_tests():
    marker = object()
    captured = {}

    def installer(**kwargs):
        captured.update(kwargs)
        return marker

    from app.core.config import Settings

    exporter = object()
    handle = configure_telemetry(
        app=FastAPI(),
        engine=object(),
        config=Settings(
            _env_file=None,
            otel_enabled=True,
            otel_exporter_otlp_traces_endpoint="http://collector:4318/v1/traces",
        ),
        installer=installer,
        exporter=exporter,
    )

    assert handle is marker
    assert captured["exporter"] is exporter


def test_configure_telemetry_installs_only_once_per_application():
    from app.core.config import Settings

    calls = []
    marker = object()
    app = FastAPI()
    config = Settings(
        _env_file=None,
        otel_enabled=True,
        otel_exporter_otlp_traces_endpoint="http://collector:4318/v1/traces",
    )

    first = configure_telemetry(
        app=app,
        engine=object(),
        config=config,
        installer=lambda **kwargs: calls.append(kwargs) or marker,
    )
    second = configure_telemetry(
        app=app,
        engine=object(),
        config=config,
        installer=lambda **kwargs: calls.append(kwargs) or object(),
    )

    assert first is marker
    assert second is marker
    assert len(calls) == 1


def test_telemetry_runtime_shutdown_is_idempotent_and_failure_isolated():
    calls = []

    class Provider:
        def force_flush(self, *, timeout_millis):
            calls.append(("flush", timeout_millis))
            raise RuntimeError("exporter failed")

        def shutdown(self):
            calls.append(("provider_shutdown", None))

    class FastAPIInstrumentor:
        def uninstrument_app(self, app):
            calls.append(("fastapi", app))

    class Instrumentor:
        def __init__(self, name):
            self.name = name

        def uninstrument(self):
            calls.append((self.name, None))

    app = object()
    runtime = TelemetryRuntime(
        app=app,
        provider=Provider(),
        fastapi_instrumentor=FastAPIInstrumentor(),
        httpx_instrumentor=Instrumentor("httpx"),
        sqlalchemy_instrumentor=Instrumentor("sqlalchemy"),
    )

    runtime.shutdown()
    runtime.shutdown()

    assert calls == [
        ("fastapi", app),
        ("httpx", None),
        ("sqlalchemy", None),
        ("flush", 5_000),
        ("provider_shutdown", None),
    ]


def test_configure_worker_telemetry_is_noop_when_disabled():
    from app.core.config import Settings

    calls = []
    handle = configure_worker_telemetry(
        engine=object(),
        config=Settings(_env_file=None, otel_enabled=False),
        installer=lambda **kwargs: calls.append(kwargs),
    )

    assert handle is None
    assert calls == []


def test_configure_worker_telemetry_uses_worker_service_name():
    from app.core.config import Settings

    marker = object()
    captured = {}

    def installer(**kwargs):
        captured.update(kwargs)
        return marker

    engine = object()
    config = Settings(
        _env_file=None,
        otel_enabled=True,
        otel_exporter_otlp_traces_endpoint="http://collector:4318/v1/traces",
        otel_trace_sample_ratio=0.5,
    )

    handle = configure_worker_telemetry(
        engine=engine,
        config=config,
        installer=installer,
    )

    assert handle is marker
    assert captured == {
        "engine": engine,
        "service_name": "bs-agent-service-worker",
        "environment": "local",
        "endpoint": "http://collector:4318/v1/traces",
        "sample_ratio": 0.5,
    }
