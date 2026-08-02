import contextvars
import hashlib
import logging
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pythonjsonlogger.json import JsonFormatter
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.security.principal import Principal
from app.tools.redaction import redact_sensitive_data, sanitize_trace_error


REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_request_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "request_log_context",
    default={},
)
_access_logger = logging.getLogger("bs_agent.access")


class SafeJsonFormatter(JsonFormatter):
    def process_log_record(self, log_record: dict[str, Any]) -> dict[str, Any]:
        redacted = redact_sensitive_data(log_record)
        return _sanitize_log_value(redacted)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = _request_id_from_scope(scope)
        token = _request_context.set({"request_id": request_id})
        started_at = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self._app(scope, receive, send_with_request_id)
        finally:
            context = _request_context.get()
            route = scope.get("route")
            route_template = getattr(route, "path", "unmatched")
            _access_logger.info(
                "http_request_completed",
                extra={
                    **context,
                    "http_method": scope["method"],
                    "route": route_template,
                    "status_code": status_code,
                    "duration_ms": round(
                        (time.perf_counter() - started_at) * 1000,
                        3,
                    ),
                },
            )
            _request_context.reset(token)


def bind_principal_log_context(principal: Principal) -> None:
    current = dict(_request_context.get())
    current.update(
        {
            "tenant_hash": _digest(principal.tenant_id),
            "subject_hash": _digest(principal.user_id),
        }
    )
    _request_context.set(current)


def configure_logging(
    *,
    log_format: str,
    level: str,
    stream: Any | None = None,
) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    if log_format == "json":
        handler.setFormatter(
            SafeJsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    setattr(handler, "_bs_agent_managed", True)
    root = logging.getLogger()
    root.handlers = [
        existing
        for existing in root.handlers
        if not getattr(existing, "_bs_agent_managed", False)
    ]
    root.addHandler(handler)
    root.setLevel(level.upper())
    _access_logger.disabled = False


def configure_telemetry(
    *,
    app: Any,
    engine: Any,
    config: Any,
    installer: Any | None = None,
    exporter: Any | None = None,
) -> Any | None:
    if not config.otel_enabled:
        return None
    if getattr(app.state, "_bs_agent_telemetry_configured", False):
        return app.state._bs_agent_telemetry_runtime
    selected_installer = installer or _install_opentelemetry
    installer_kwargs = {
        "app": app,
        "engine": engine,
        "service_name": config.service_name,
        "environment": config.app_env,
        "endpoint": config.otel_exporter_otlp_traces_endpoint,
        "sample_ratio": config.otel_trace_sample_ratio,
    }
    if exporter is not None:
        installer_kwargs["exporter"] = exporter
    runtime = selected_installer(**installer_kwargs)
    app.state._bs_agent_telemetry_runtime = runtime
    app.state._bs_agent_telemetry_configured = True
    return runtime


def configure_worker_telemetry(
    *,
    engine: Any,
    config: Any,
    installer: Any | None = None,
) -> Any | None:
    if not config.otel_enabled:
        return None
    selected_installer = installer or _install_worker_opentelemetry
    return selected_installer(
        engine=engine,
        service_name=f"{config.service_name}-worker",
        environment=config.app_env,
        endpoint=config.otel_exporter_otlp_traces_endpoint,
        sample_ratio=config.otel_trace_sample_ratio,
    )


@dataclass
class TelemetryRuntime:
    app: Any
    provider: Any
    fastapi_instrumentor: Any
    httpx_instrumentor: Any
    sqlalchemy_instrumentor: Any
    _shutdown: bool = field(default=False, init=False, repr=False)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        _run_telemetry_cleanup(
            lambda: self.fastapi_instrumentor.uninstrument_app(self.app),
            self.httpx_instrumentor.uninstrument,
            self.sqlalchemy_instrumentor.uninstrument,
            lambda: self.provider.force_flush(timeout_millis=5_000),
            self.provider.shutdown,
        )


@dataclass
class WorkerTelemetryRuntime:
    provider: Any
    celery_instrumentor: Any
    httpx_instrumentor: Any
    sqlalchemy_instrumentor: Any
    _shutdown: bool = field(default=False, init=False, repr=False)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        _run_telemetry_cleanup(
            self.celery_instrumentor.uninstrument,
            self.httpx_instrumentor.uninstrument,
            self.sqlalchemy_instrumentor.uninstrument,
            lambda: self.provider.force_flush(timeout_millis=5_000),
            self.provider.shutdown,
        )


def _run_telemetry_cleanup(*callbacks: Any) -> None:
    for callback in callbacks:
        try:
            callback()
        except Exception:
            logging.getLogger("bs_agent.telemetry").warning(
                "telemetry_cleanup_failed"
            )


def _install_opentelemetry(
    *,
    app: Any,
    engine: Any,
    service_name: str,
    environment: str,
    endpoint: str,
    sample_ratio: float,
    exporter: Any | None = None,
) -> TelemetryRuntime:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "deployment.environment.name": environment,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(exporter or OTLPSpanExporter(endpoint=endpoint))
    )
    trace.set_tracer_provider(provider)
    fastapi_instrumentor = FastAPIInstrumentor()
    httpx_instrumentor = HTTPXClientInstrumentor()
    sqlalchemy_instrumentor = SQLAlchemyInstrumentor()
    fastapi_instrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls="/health/live,/health/ready",
    )
    httpx_instrumentor.instrument(tracer_provider=provider)
    sqlalchemy_instrumentor.instrument(
        engine=engine,
        tracer_provider=provider,
    )
    return TelemetryRuntime(
        app=app,
        provider=provider,
        fastapi_instrumentor=fastapi_instrumentor,
        httpx_instrumentor=httpx_instrumentor,
        sqlalchemy_instrumentor=sqlalchemy_instrumentor,
    )


def _install_worker_opentelemetry(
    *,
    engine: Any,
    service_name: str,
    environment: str,
    endpoint: str,
    sample_ratio: float,
) -> WorkerTelemetryRuntime:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "deployment.environment.name": environment,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    )
    trace.set_tracer_provider(provider)
    celery_instrumentor = CeleryInstrumentor()
    httpx_instrumentor = HTTPXClientInstrumentor()
    sqlalchemy_instrumentor = SQLAlchemyInstrumentor()
    celery_instrumentor.instrument(tracer_provider=provider)
    httpx_instrumentor.instrument(tracer_provider=provider)
    sqlalchemy_instrumentor.instrument(engine=engine, tracer_provider=provider)
    return WorkerTelemetryRuntime(
        provider=provider,
        celery_instrumentor=celery_instrumentor,
        httpx_instrumentor=httpx_instrumentor,
        sqlalchemy_instrumentor=sqlalchemy_instrumentor,
    )


def _request_id_from_scope(scope: Scope) -> str:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == b"x-request-id":
            candidate = raw_value.decode("ascii", errors="ignore")
            if REQUEST_ID_PATTERN.fullmatch(candidate):
                return candidate
            break
    return uuid4().hex


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _sanitize_log_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_trace_error(value, max_length=2048)
    if isinstance(value, Mapping):
        return {key: _sanitize_log_value(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_sanitize_log_value(item) for item in value]
    return value
