from __future__ import annotations

import hashlib
import logging
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler
from langfuse.types import (
    MaskOtelSpansParams,
    MaskOtelSpansResult,
    OtelSpanPatch,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from app.core.config import Settings

logger = logging.getLogger("bs_agent.langfuse")

_active_runtime: LangfuseRuntime | None = None
_IDENTIFIER_DOMAIN = b"bs-agent:langfuse:v1:"
_EXACT_SENSITIVE_ATTRIBUTES = frozenset(
    {
        "langfuse.observation.input",
        "langfuse.observation.output",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "input.value",
        "output.value",
        "mlflow.spaninputs",
        "mlflow.spanoutputs",
    }
)
_SENSITIVE_ATTRIBUTE_PREFIXES = (
    "gen_ai.prompt.",
    "gen_ai.completion.",
    "gen_ai.input.messages.",
    "gen_ai.output.messages.",
)
_SAFE_REASONING_USAGE_PREFIXES = (
    "gen_ai.usage.",
    "langfuse.observation.usage_details",
)


@dataclass
class LangfuseRuntime:
    client: Any
    provider: Any
    public_key: str
    environment: str
    handler_factory: Callable[..., Any]
    propagate_factory: Callable[..., Any] = propagate_attributes
    _shutdown: bool = field(default=False, init=False, repr=False)

    def new_handler(self) -> Any:
        return self.handler_factory(public_key=self.public_key)

    def shutdown(self) -> None:
        global _active_runtime
        if self._shutdown:
            return
        self._shutdown = True
        if _active_runtime is self:
            _active_runtime = None
        _safe_runtime_call(
            "langfuse_force_flush_failed",
            lambda: self.provider.force_flush(timeout_millis=5_000),
        )
        _safe_runtime_call("langfuse_client_shutdown_failed", self.client.shutdown)
        _safe_runtime_call("langfuse_provider_shutdown_failed", self.provider.shutdown)


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

    selected_provider_factory = provider_factory or _create_tracer_provider
    provider = selected_provider_factory(
        service_name=config.service_name,
        environment=config.app_env,
    )
    selected_client_factory = client_factory or Langfuse
    client = selected_client_factory(
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


def mask_langfuse_otel_spans(
    *,
    params: MaskOtelSpansParams,
) -> MaskOtelSpansResult:
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
    if normalized in _EXACT_SENSITIVE_ATTRIBUTES:
        return True
    if normalized.startswith(_SENSITIVE_ATTRIBUTE_PREFIXES):
        return True
    return "reasoning" in normalized and not normalized.startswith(
        _SAFE_REASONING_USAGE_PREFIXES
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

    observation_manager = None
    propagation_manager = None
    observation_entered = False
    propagation_entered = False
    try:
        handler = runtime.new_handler()
        trace_id = runtime.client.create_trace_id(seed=business_trace_id)
        observation_manager = runtime.client.start_as_current_observation(
            as_type="agent",
            name="bs-agent.run",
            trace_context={"trace_id": trace_id},
            metadata={
                "business_trace_id": business_trace_id,
                "execution_segment": execution_segment,
                "tenant_hash": hash_langfuse_identifier(tenant_id),
            },
        )
        observation = observation_manager.__enter__()
        observation_entered = True
        propagation_manager = runtime.propagate_factory(
            session_id=hash_langfuse_identifier(session_key),
            environment=runtime.environment,
        )
        propagation_manager.__enter__()
        propagation_entered = True
    except Exception as exc:  # noqa: BLE001 - 观测故障不能中断业务执行
        if propagation_entered:
            _safe_context_exit(propagation_manager, (None, None, None))
        if observation_entered:
            _safe_context_exit(observation_manager, (None, None, None))
        _log_runtime_failure("langfuse_agent_run_start_failed", exc)
        yield ()
        return

    try:
        yield (handler,)
    except BaseException:
        exc_info = sys.exc_info()
        _safe_context_exit(propagation_manager, exc_info)
        _safe_context_exit(observation_manager, exc_info)
        raise
    else:
        _safe_context_exit(propagation_manager, (None, None, None))
        _safe_context_exit(observation_manager, (None, None, None))
    finally:
        del observation


@contextmanager
def langfuse_generation(
    *,
    name: str,
    model: str,
    model_parameters: Mapping[str, str | int | float | bool | None],
    metadata: Mapping[str, Any] | None = None,
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
            metadata=dict(metadata or {}),
        )
        generation = manager.__enter__()
    except Exception as exc:  # noqa: BLE001 - 观测故障不能中断业务执行
        _log_runtime_failure("langfuse_generation_start_failed", exc)
        yield None
        return

    try:
        yield generation
    except BaseException:
        exc_info = sys.exc_info()
        _safe_context_exit(manager, exc_info)
        raise
    else:
        _safe_context_exit(manager, (None, None, None))


def update_langfuse_generation(generation: Any | None, **attributes: Any) -> None:
    if generation is None:
        return
    try:
        generation.update(**attributes)
    except Exception as exc:  # noqa: BLE001 - 观测故障不能中断业务执行
        _log_runtime_failure("langfuse_generation_update_failed", exc)


def _safe_context_exit(manager: Any, exc_info: tuple[Any, Any, Any]) -> None:
    try:
        manager.__exit__(*exc_info)
    except Exception as exc:  # noqa: BLE001 - 关闭观测上下文不能覆盖业务结果
        _log_runtime_failure("langfuse_context_close_failed", exc)


def _safe_runtime_call(event: str, callback: Callable[[], Any]) -> None:
    try:
        callback()
    except Exception as exc:  # noqa: BLE001 - 关闭观测资源不能阻止应用退出
        _log_runtime_failure(event, exc)


def _log_runtime_failure(event: str, exc: Exception) -> None:
    logger.warning(event, extra={"error_type": type(exc).__name__})
