from __future__ import annotations

import hashlib
import logging
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from langfuse.langchain import CallbackHandler
from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanPatch
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from app.core.config import Settings
from langfuse import Langfuse

logger = logging.getLogger("bs_agent.langfuse")

_active_runtime: LangfuseRuntime | None = None
_IDENTIFIER_DOMAIN = b"bs-agent:langfuse:v1:"
_SENSITIVE_ATTRIBUTES = frozenset(
    {
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.trace.input",
        "langfuse.trace.output",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "input.value",
        "output.value",
    }
)
_SENSITIVE_PREFIXES = (
    "gen_ai.prompt.",
    "gen_ai.completion.",
    "gen_ai.input.messages.",
    "gen_ai.output.messages.",
)


@dataclass
class LangfuseRuntime:
    client: Any
    provider: Any
    public_key: str
    environment: str
    handler_factory: Callable[..., Any]
    _shutdown: bool = field(default=False, init=False, repr=False)

    def new_handler(self, *, trace_context: Mapping[str, str]) -> Any:
        return self.handler_factory(
            public_key=self.public_key,
            trace_context=dict(trace_context),
        )

    def shutdown(self) -> None:
        global _active_runtime
        if self._shutdown:
            return
        self._shutdown = True
        if _active_runtime is self:
            _active_runtime = None
        _safe_call(
            "langfuse_force_flush_failed",
            lambda: self.provider.force_flush(timeout_millis=5_000),
        )
        _safe_call("langfuse_client_shutdown_failed", self.client.shutdown)
        _safe_call("langfuse_provider_shutdown_failed", self.provider.shutdown)


def configure_langfuse(
    *,
    config: Settings,
    client_factory: Callable[..., Any] | None = None,
    provider_factory: Callable[..., Any] | None = None,
    handler_factory: Callable[..., Any] | None = None,
) -> LangfuseRuntime | None:
    global _active_runtime
    if not config.langfuse_enabled:
        return None

    provider = (provider_factory or _create_tracer_provider)(
        service_name=config.service_name,
        environment=config.app_env,
    )
    client = (client_factory or Langfuse)(
        public_key=config.langfuse_public_key,
        secret_key=config.langfuse_secret_key,
        base_url=config.langfuse_base_url,
        environment=config.app_env,
        sample_rate=config.langfuse_sample_rate,
        tracer_provider=provider,
        mask_otel_spans=mask_langfuse_otel_spans,
    )
    runtime = LangfuseRuntime(
        client=client,
        provider=provider,
        public_key=config.langfuse_public_key,
        environment=config.app_env,
        handler_factory=handler_factory or CallbackHandler,
    )
    _active_runtime = runtime
    return runtime


def _create_tracer_provider(*, service_name: str, environment: str) -> TracerProvider:
    return TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "deployment.environment.name": environment,
            }
        )
    )


def hash_langfuse_identifier(value: str) -> str:
    digest = hashlib.sha256()
    digest.update(_IDENTIFIER_DOMAIN)
    digest.update(value.encode("utf-8"))
    return digest.hexdigest()


def mask_langfuse_otel_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult:
    patches = {}
    for identifier, span in params.spans.items():
        deleted = tuple(
            key
            for key in span.attributes
            if _is_sensitive_attribute(str(key))
        )
        if deleted:
            patches[identifier] = OtelSpanPatch(delete_attributes=deleted)
    return MaskOtelSpansResult(span_patches=patches)


def _is_sensitive_attribute(key: str) -> bool:
    normalized = key.lower()
    return (
        normalized in _SENSITIVE_ATTRIBUTES
        or normalized.startswith(_SENSITIVE_PREFIXES)
        or (
            "reasoning" in normalized
            and not normalized.startswith(
                ("gen_ai.usage.", "langfuse.observation.usage_details")
            )
        )
    )


@contextmanager
def langfuse_agent_run(
    *,
    business_trace_id: str,
    session_key: str,
    tenant_id: str,
    execution_segment: str,
) -> Iterator[tuple[Any, ...]]:
    runtime = _active_runtime
    if runtime is None:
        yield ()
        return

    observation = None
    try:
        trace_id = runtime.client.create_trace_id(seed=business_trace_id)
        observation = runtime.client.start_observation(
            as_type="agent",
            name="bs-agent.run",
            trace_context={"trace_id": trace_id},
            metadata={
                "business_trace_id": business_trace_id,
                "execution_segment": execution_segment,
                "tenant_hash": hash_langfuse_identifier(tenant_id),
                "session_hash": hash_langfuse_identifier(session_key),
            },
        )
        handler = runtime.new_handler(
            trace_context={
                "trace_id": trace_id,
                "parent_span_id": observation.id,
            }
        )
    except Exception as exc:  # noqa: BLE001 - 观测失败不得中断业务
        if observation is not None:
            _safe_call("langfuse_agent_run_close_failed", observation.end)
        _log_failure("langfuse_agent_run_start_failed", exc)
        yield ()
        return

    try:
        yield (handler,)
    except BaseException:
        _safe_call("langfuse_agent_run_close_failed", observation.end)
        raise
    else:
        _safe_call("langfuse_agent_run_close_failed", observation.end)


@contextmanager
def langfuse_generation(
    *,
    name: str,
    model: str,
    model_parameters: Mapping[str, str | int | float | bool | None],
) -> Iterator[Any | None]:
    runtime = _active_runtime
    if runtime is None:
        yield None
        return
    try:
        manager = runtime.client.start_as_current_observation(
            as_type="generation",
            name=name,
            model=model,
            model_parameters=dict(model_parameters),
        )
        generation = manager.__enter__()
    except Exception as exc:  # noqa: BLE001 - 观测失败不得中断业务
        _log_failure("langfuse_generation_start_failed", exc)
        yield None
        return

    try:
        yield generation
    except BaseException:
        _safe_context_exit(manager, sys.exc_info())
        raise
    else:
        _safe_context_exit(manager, (None, None, None))


def update_langfuse_generation(generation: Any | None, **attributes: Any) -> None:
    if generation is None:
        return
    try:
        generation.update(**attributes)
    except Exception as exc:  # noqa: BLE001 - 观测失败不得中断业务
        _log_failure("langfuse_generation_update_failed", exc)


def _safe_context_exit(manager: Any, exc_info: tuple[Any, Any, Any]) -> None:
    try:
        manager.__exit__(*exc_info)
    except Exception as exc:  # noqa: BLE001 - 观测关闭失败不得中断业务
        _log_failure("langfuse_context_close_failed", exc)


def _safe_call(event: str, callback: Callable[[], Any]) -> None:
    try:
        callback()
    except Exception as exc:  # noqa: BLE001 - 观测关闭失败不得中断业务
        _log_failure(event, exc)


def _log_failure(event: str, exc: Exception) -> None:
    logger.warning(event, extra={"error_type": type(exc).__name__})
