from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from langfuse.langchain import CallbackHandler
from langfuse.types import (
    MaskOtelSpansParams,
    MaskOtelSpansResult,
    OtelSpanPatch,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from app.core.config import Settings
from app.operations.langfuse_content import LangfuseContentPolicy
from app.operations.langfuse_langchain_content import (
    sanitized_content_json,
    sanitized_langchain_io,
)
from app.operations.safe_exporter import fixed_error_status, should_export_langfuse_span
from app.security.observation import observation_view
from langfuse import Langfuse, propagate_attributes

logger = logging.getLogger("bs_agent.langfuse")

_active_runtime: LangfuseRuntime | None = None
_IDENTIFIER_DOMAIN = b"bs-agent:langfuse:v1:"
_EXACT_SENSITIVE_ATTRIBUTES = frozenset(
    {
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.trace.input",
        "langfuse.trace.output",
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
_METADATA_PREFIXES = (
    "langfuse.observation.metadata",
    "langfuse.trace.metadata",
)
_SAFE_METADATA_KEYS = frozenset(
    {
        "business_trace_id",
        "execution_segment",
        "tenant_hash",
        "request_mode",
        "input_chars",
        "system_prompt_chars",
        "user_prompt_chars",
        "response_chars",
        "content_chars",
        "reasoning_chars",
        "ttft_ms",
        "timeout_seconds",
        "first_token_received",
        "first_token_kind",
        "finish_reason",
        "timeout_phase",
        "is_langchain_root",
        "langgraph_node",
        "langgraph_step",
        "content_policy_applied",
        "search_provider",
        "search_http_status",
        "search_error_code",
        "search_result_count",
        "search_skipped_count",
        "search_truncated",
        "search_duration_ms",
    }
)
_SAFE_ERROR_STATUS = re.compile(
    r"llm_(?:http_error|timeout|unreachable|invalid_response)"
    r"(?::(?:connect|read|write|pool|unknown))?"
    r"(?::(?:before_first_token|after_first_token))?"
)


@dataclass
class LangfuseRuntime:
    client: Any
    provider: Any
    public_key: str
    environment: str
    handler_factory: Callable[..., Any]
    propagate_factory: Callable[..., Any] = propagate_attributes
    content_policy: LangfuseContentPolicy = field(default_factory=LangfuseContentPolicy)
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
    content_policy = LangfuseContentPolicy(
        mode=getattr(config, "langfuse_content_mode", "local_content"),
        max_chars=getattr(config, "langfuse_max_content_chars", 20_000),
        capture_reasoning=getattr(config, "langfuse_capture_reasoning", False),
    )
    selected_client_factory = client_factory or Langfuse
    client = selected_client_factory(
        public_key=config.langfuse_public_key,
        secret_key=config.langfuse_secret_key,
        base_url=config.langfuse_base_url,
        environment=config.app_env,
        sample_rate=config.langfuse_sample_rate,
        tracer_provider=provider,
        should_export_span=should_export_langfuse_span,
        mask_otel_spans=lambda *, params: mask_langfuse_otel_spans(
            params=params,
            content_policy=content_policy,
        ),
    )
    runtime = LangfuseRuntime(
        client=client,
        provider=provider,
        public_key=config.langfuse_public_key,
        environment=config.app_env,
        handler_factory=handler_factory or CallbackHandler,
        content_policy=content_policy,
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
    content_mode: str = "local_content",
    content_policy: LangfuseContentPolicy | None = None,
) -> MaskOtelSpansResult:
    policy = content_policy or LangfuseContentPolicy(mode=content_mode)
    patches = {}
    for identifier, span in params.spans.items():
        content_policy_applied = (
            span.attributes.get("langfuse.observation.metadata.content_policy_applied")
            is True
            and policy.mode != "metadata_only"
        )
        deleted = tuple(
            key
            for key in span.attributes
            if _is_sensitive_attribute(
                str(key),
                span.attributes[key],
                content_policy_applied=content_policy_applied,
            )
        )
        try:
            safe_io = sanitized_langchain_io(span, policy=policy)
        except Exception as exc:  # noqa: BLE001 - 转换故障时仍删除原始内容
            _log_runtime_failure("langfuse_callback_content_failed", exc)
            safe_io = {}
        retained = {key: value for key, value in span.attributes.items() if key not in deleted}
        checked = observation_view({**retained, **safe_io})
        if checked != retained or deleted:
            removed = tuple(key for key in span.attributes if key not in checked or key in deleted)
            updates = {key: value for key, value in checked.items()
                       if key in safe_io or key not in retained or value != retained[key]}
            # SDK 先删除再覆盖；不标记原始正文为可信，只放行转换后的导出副本。
            patches[identifier] = OtelSpanPatch(
                delete_attributes=removed, set_attributes=updates,
            )
    return MaskOtelSpansResult(span_patches=patches)


def _is_sensitive_attribute(
    key: str, value: Any = None, *, content_policy_applied: bool = False
) -> bool:
    normalized = key.lower()
    for prefix in _METADATA_PREFIXES:
        if normalized == prefix:
            return True
        if normalized.startswith(prefix + "."):
            return normalized[len(prefix) + 1 :] not in _SAFE_METADATA_KEYS
    if normalized == "langfuse.observation.status_message":
        return not isinstance(value, str) or _SAFE_ERROR_STATUS.fullmatch(value) is None
    if normalized in _EXACT_SENSITIVE_ATTRIBUTES:
        return not (
            content_policy_applied
            and normalized
            in {
                "langfuse.observation.input",
                "langfuse.observation.output",
                "langfuse.trace.input",
                "langfuse.trace.output",
            }
        )
    if normalized.startswith(_SENSITIVE_ATTRIBUTE_PREFIXES):
        return True
    return "reasoning" in normalized and not normalized.startswith(_SAFE_REASONING_USAGE_PREFIXES)


@contextmanager
def langfuse_agent_run(
    *,
    business_trace_id: str,
    session_key: str,
    tenant_id: str,
    execution_segment: str,
    user_id: str | None = None,
    input: Any = None,
) -> Iterator[LangfuseRunHandle]:
    runtime = _active_runtime
    if runtime is None:
        yield LangfuseRunHandle(content_policy=LangfuseContentPolicy())
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
            **_observation_input(runtime.content_policy, input),
            metadata=_safe_metadata(
                runtime.content_policy,
                {
                    "business_trace_id": business_trace_id,
                    "execution_segment": execution_segment,
                    "tenant_hash": hash_langfuse_identifier(tenant_id),
                },
            ),
        )
        observation = observation_manager.__enter__()
        observation_entered = True
        propagation_manager = runtime.propagate_factory(
            session_id=hash_langfuse_identifier(session_key),
            user_id=hash_langfuse_identifier(user_id) if user_id else None,
            tags=[f"business_trace_id:{hash_langfuse_identifier(business_trace_id)}"],
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
        yield LangfuseRunHandle(content_policy=runtime.content_policy)
        return

    try:
        yield LangfuseRunHandle(
            callbacks=(handler,),
            trace_id=trace_id,
            observation_id=getattr(observation, "id", None),
            observation=observation,
            content_policy=runtime.content_policy,
        )
    except BaseException as exc:
        update_langfuse_observation(observation, level="ERROR", status_message=fixed_error_status(exc))
        _safe_context_exit(propagation_manager, (None, None, None))
        _safe_context_exit(observation_manager, (None, None, None))
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
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    input: Any = None,
) -> Iterator[Any | None]:
    runtime = _active_runtime
    if runtime is None:
        yield None
        return
    observation_input = input
    if observation_input is None and (system_prompt is not None or user_prompt is not None):
        observation_input = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        }
    safe_input = {}
    if observation_input is not None and runtime.content_policy.mode != "metadata_only":
        try:
            safe_input["input"] = json.loads(sanitized_content_json(
                observation_input, policy=runtime.content_policy,
            ))
        except Exception as exc:  # noqa: BLE001 - 转换失败只丢弃正文，保留模型与用量观测
            _log_runtime_failure("langfuse_generation_input_failed", exc)
    try:
        manager = runtime.client.start_as_current_observation(
            as_type="generation",
            name=name,
            model=model,
            model_parameters=dict(model_parameters),
            metadata=_safe_metadata(runtime.content_policy, metadata),
            **safe_input,
        )
        generation = manager.__enter__()
    except Exception as exc:  # noqa: BLE001 - 观测故障不能中断业务执行
        _log_runtime_failure("langfuse_generation_start_failed", exc)
        yield None
        return

    try:
        yield generation
    except BaseException as exc:
        update_langfuse_generation(generation, level="ERROR", status_message=fixed_error_status(exc))
        _safe_context_exit(manager, (None, None, None))
        raise
    else:
        _safe_context_exit(manager, (None, None, None))


@dataclass(eq=False)
class LangfuseRunHandle:
    callbacks: tuple[Any, ...] = ()
    trace_id: str | None = None
    observation_id: str | None = None
    observation: Any | None = None
    content_policy: LangfuseContentPolicy = field(default_factory=LangfuseContentPolicy)

    def __iter__(self):
        return iter(self.callbacks)

    def __len__(self) -> int:
        return len(self.callbacks)

    def __bool__(self) -> bool:
        return bool(self.callbacks)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple):
            return self.callbacks == other
        if isinstance(other, LangfuseRunHandle):
            return (
                self.callbacks,
                self.trace_id,
                self.observation_id,
                self.observation,
            ) == (
                other.callbacks,
                other.trace_id,
                other.observation_id,
                other.observation,
            )
        return NotImplemented

    def update(self, **attributes: Any) -> None:
        if self.observation is None:
            return
        _safe_update_observation(
            self.observation,
            content_policy=self.content_policy,
            attributes=attributes,
            event="langfuse_observation_update_failed",
        )


@contextmanager
def langfuse_observation(
    *,
    as_type: str,
    name: str,
    input: Any = None,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[Any | None]:
    runtime = _active_runtime
    if runtime is None:
        yield None
        return
    manager = None
    try:
        manager = runtime.client.start_as_current_observation(
            as_type=as_type,
            name=name,
            **_observation_input(runtime.content_policy, input),
            metadata=_safe_metadata(runtime.content_policy, metadata),
        )
        observation = manager.__enter__()
    except Exception as exc:  # noqa: BLE001 - 观测故障不能中断业务执行
        _log_runtime_failure("langfuse_observation_start_failed", exc)
        yield None
        return
    try:
        yield observation
    except BaseException as exc:
        update_langfuse_observation(observation, level="ERROR", status_message=fixed_error_status(exc))
        _safe_context_exit(manager, (None, None, None))
        raise
    else:
        _safe_context_exit(manager, (None, None, None))


def update_langfuse_generation(generation: Any | None, **attributes: Any) -> None:
    if generation is None:
        return
    runtime = _active_runtime
    if runtime is None:
        policy = LangfuseContentPolicy()
    else:
        policy = runtime.content_policy
    try:
        attributes = _sanitize_observation_attributes(attributes, policy)
        generation.update(**attributes)
    except Exception as exc:  # noqa: BLE001 - 观测故障不能中断业务执行
        _log_runtime_failure("langfuse_generation_update_failed", exc)


def update_langfuse_observation(
    observation: Any | None, *, bounded_content: bool = False, **attributes: Any,
) -> None:
    """通过内容策略更新 observation，避免工具结果绕过脱敏。"""
    if observation is None:
        return
    runtime = _active_runtime
    policy = runtime.content_policy if runtime is not None else LangfuseContentPolicy()
    if bounded_content:
        for field in ("input", "output"):
            if field not in attributes:
                continue
            value = attributes.pop(field)
            if policy.mode == "metadata_only":
                continue
            try:
                attributes[field] = sanitized_content_json(value, policy=policy)
            except Exception as exc:  # noqa: BLE001 - 转换失败时丢弃正文而非回退原值
                _log_runtime_failure("langfuse_observation_content_failed", exc)
    _safe_update_observation(
        observation,
        content_policy=policy,
        attributes=attributes,
        event="langfuse_observation_update_failed",
    )


def _safe_metadata(
    policy: LangfuseContentPolicy, metadata: Mapping[str, Any] | None
) -> dict[str, Any]:
    safe_metadata = policy.sanitize(dict(metadata or {}))
    if not isinstance(safe_metadata, dict):
        safe_metadata = {}
    # 这些是有界统计字段，不能因为名称包含 token/reasoning 被正文策略误删。
    values = metadata or {}
    if isinstance(values.get("first_token_received"), bool):
        safe_metadata["first_token_received"] = values["first_token_received"]
    if values.get("first_token_kind") in ("content", "reasoning"):
        safe_metadata["first_token_kind"] = values["first_token_kind"]
    reasoning_chars = values.get("reasoning_chars")
    if isinstance(reasoning_chars, int) and not isinstance(reasoning_chars, bool) and reasoning_chars >= 0:
        safe_metadata["reasoning_chars"] = reasoning_chars
    safe_metadata["content_policy_applied"] = True
    return safe_metadata


def _observation_input(policy: LangfuseContentPolicy, value: Any) -> dict[str, Any]:
    if policy.mode == "metadata_only" or value is None:
        return {}
    return {"input": policy.sanitize(value)}


def _sanitize_observation_attributes(
    attributes: Mapping[str, Any], policy: LangfuseContentPolicy
) -> dict[str, Any]:
    safe_attributes = {
        key: policy.sanitize(value)
        for key, value in attributes.items()
        if key != "metadata"
        and not (policy.mode == "metadata_only" and key in {"input", "output"})
    }
    status_message = safe_attributes.get("status_message")
    if isinstance(status_message, dict) and isinstance(
        status_message.get("content"), str
    ):
        safe_attributes["status_message"] = status_message["content"]
    safe_attributes["metadata"] = _safe_metadata(
        policy,
        attributes.get("metadata")
        if isinstance(attributes.get("metadata"), Mapping)
        else None,
    )
    return safe_attributes


def _safe_update_observation(
    observation: Any,
    *,
    content_policy: LangfuseContentPolicy,
    attributes: Mapping[str, Any],
    event: str,
) -> None:
    try:
        safe_attributes = _sanitize_observation_attributes(attributes, content_policy)
        observation.update(**safe_attributes)
    except Exception as exc:  # noqa: BLE001 - 观测故障不能中断业务执行
        _log_runtime_failure(event, exc)


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
