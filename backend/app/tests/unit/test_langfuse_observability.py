from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.operations import langfuse_observability
from app.operations.langfuse_observability import (
    LangfuseRunHandle,
    configure_langfuse,
    hash_langfuse_identifier,
    langfuse_agent_run,
    langfuse_generation,
    langfuse_observation,
    mask_langfuse_otel_spans,
    update_langfuse_generation,
)
from langfuse.types import (
    MaskOtelSpansParams,
    OtelSpanData,
    OtelSpanIdentifier,
)


def _config(**overrides):
    values = {
        "langfuse_enabled": True,
        "langfuse_public_key": "pk-test",
        "langfuse_secret_key": "sk-test",
        "langfuse_base_url": "https://cloud.langfuse.com",
        "langfuse_sample_rate": 1.0,
        "langfuse_content_mode": "local_content",
        "langfuse_capture_reasoning": False,
        "langfuse_max_content_chars": 1000,
        "service_name": "bs-agent-service",
        "app_env": "test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Provider:
    def __init__(self):
        self.flush_calls = []
        self.shutdown_calls = 0

    def force_flush(self, *, timeout_millis):
        self.flush_calls.append(timeout_millis)

    def shutdown(self):
        self.shutdown_calls += 1


class _Client:
    def __init__(self):
        self.shutdown_calls = 0
        self.observations = []
        self.observation_exit_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1

    def create_trace_id(self, *, seed):
        return f"trace:{seed}"

    def start_as_current_observation(self, **kwargs):
        self.observations.append(kwargs)
        observation_data = kwargs

        @contextmanager
        def observation_manager():
            try:
                yield SimpleNamespace(
                    id="observation-id",
                    update=lambda **attributes: observation_data.setdefault(
                        "updates", []
                    ).append(attributes),
                )
            finally:
                self.observation_exit_calls += 1

        return observation_manager()


def test_configure_langfuse_is_noop_when_disabled():
    calls = []

    runtime = configure_langfuse(
        config=_config(langfuse_enabled=False),
        client_factory=lambda **kwargs: calls.append(kwargs),
    )

    assert runtime is None
    assert calls == []


def test_configure_langfuse_passes_official_sdk_configuration():
    client = _Client()
    provider = _Provider()
    client_calls = []
    handler_calls = []

    runtime = configure_langfuse(
        config=_config(),
        client_factory=lambda **kwargs: client_calls.append(kwargs) or client,
        provider_factory=lambda **kwargs: provider,
        handler_factory=lambda **kwargs: handler_calls.append(kwargs) or object(),
    )

    assert runtime is not None
    assert client_calls[0]["public_key"] == "pk-test"
    assert client_calls[0]["secret_key"] == "sk-test"
    assert client_calls[0]["base_url"] == "https://cloud.langfuse.com"
    assert client_calls[0]["environment"] == "test"
    assert client_calls[0]["sample_rate"] == 1.0
    assert client_calls[0]["tracer_provider"] is provider
    assert callable(client_calls[0]["mask_otel_spans"])

    with langfuse_agent_run(
        business_trace_id="business-trace",
        session_key="session-secret",
        tenant_id="tenant-secret",
        execution_segment="initial",
        user_id="user-secret",
        input={"prompt": "private prompt"},
    ) as callbacks:
        assert len(callbacks) == 1
        assert callbacks.trace_id == "trace:business-trace"
        assert callbacks.observation is not None

    assert handler_calls == [{"public_key": "pk-test"}]
    assert client.observations[0]["trace_context"] == {"trace_id": "trace:business-trace"}
    assert client.observations[0]["input"] == {"prompt": "private prompt"}
    assert client.observations[0]["as_type"] == "agent"
    assert client.observations[0]["metadata"] == {
        "business_trace_id": "business-trace",
        "execution_segment": "initial",
        "tenant_hash": hash_langfuse_identifier("tenant-secret"),
        "content_policy_applied": True,
    }
    runtime.shutdown()


def test_langfuse_generation_does_not_capture_io():
    client = _Client()
    runtime = configure_langfuse(
        config=_config(),
        client_factory=lambda **_kwargs: client,
        provider_factory=lambda **_kwargs: _Provider(),
        handler_factory=lambda **_kwargs: object(),
    )

    with langfuse_generation(
        name="deepseek.chat.completions",
        model="deepseek-v4-flash",
        model_parameters={"temperature": 0.0, "max_tokens": 256},
        metadata={"request_mode": "streaming", "input_chars": 42},
    ) as generation:
        assert generation is not None

    observation = client.observations[0]
    assert observation["as_type"] == "generation"
    assert observation["model"] == "deepseek-v4-flash"
    assert observation["metadata"] == {
        "request_mode": "streaming",
        "input_chars": 42,
        "content_policy_applied": True,
    }
    assert "input" not in observation
    assert "output" not in observation
    assert runtime is not None
    runtime.shutdown()


def test_generation_sanitizes_prompt_and_answer_and_strips_reasoning():
    client = _Client()
    runtime = configure_langfuse(
        config=_config(),
        client_factory=lambda **_kwargs: client,
        provider_factory=lambda **_kwargs: _Provider(),
        handler_factory=lambda **_kwargs: object(),
    )
    with langfuse_generation(
        name="generation",
        model="deepseek",
        model_parameters={},
        system_prompt="system prompt",
        user_prompt="user prompt",
    ) as generation:
        update_langfuse_generation(
            generation,
            output={"content": "answer", "reasoning_content": "private reasoning"}
        )

    assert client.observations[0]["input"] == {
        "system_prompt": "system prompt",
        "user_prompt": "user prompt",
    }
    assert client.observations[0]["updates"] == [
        {
            "output": {"content": "answer"},
            "metadata": {"content_policy_applied": True},
        }
    ]
    assert generation is not None
    runtime.shutdown()


def test_generation_input_conversion_failure_keeps_model_and_usage_without_raw_input():
    client = _Client()
    runtime = configure_langfuse(
        config=_config(), client_factory=lambda **_kwargs: client,
        provider_factory=lambda **_kwargs: _Provider(),
        handler_factory=lambda **_kwargs: object(),
    )
    try:
        with langfuse_generation(
            name="generation", model="deepseek", model_parameters={},
            user_prompt='用户粘贴的不完整JSON：{"password":',
        ) as generation:
            assert generation is not None
            update_langfuse_generation(generation, usage_details={"input": 100, "output": 20})
        assert len(client.observations) == 1
        observation = client.observations[0]
        assert observation["model"] == "deepseek"
        assert "input" not in observation
        assert observation["updates"][0]["usage_details"] == {"input": 100, "output": 20}
    finally:
        runtime.shutdown()


def test_local_debug_content_policy_can_capture_reasoning():
    client = _Client()
    runtime = configure_langfuse(
        config=_config(
            langfuse_content_mode="local_debug", langfuse_capture_reasoning=True
        ),
        client_factory=lambda **_kwargs: client,
        provider_factory=lambda **_kwargs: _Provider(),
    )
    with langfuse_generation(
        name="generation",
        model="deepseek",
        model_parameters={},
    ) as generation:
        update_langfuse_generation(
            generation,
            output={"content": "answer", "reasoning_content": "reasoning"},
        )
    assert client.observations[0]["updates"] == [
        {
            "output": {"content": "answer", "reasoning_content": "reasoning"},
            "metadata": {"content_policy_applied": True},
        }
    ]
    runtime.shutdown()


def test_langfuse_observation_sanitizes_input_and_isolates_failure():
    client = _Client()
    runtime = configure_langfuse(
        config=_config(),
        client_factory=lambda **_kwargs: client,
        provider_factory=lambda **_kwargs: _Provider(),
    )
    with langfuse_observation(
        as_type="tool", name="tool", input={"api_key": "secret", "value": "safe"}
    ) as observation:
        assert observation is not None
    assert client.observations[0]["input"] == {"value": "safe"}
    assert client.observations[0]["metadata"] == {"content_policy_applied": True}
    runtime.shutdown()


def test_observation_update_sanitizes_io_and_metadata_and_marks_policy():
    assert hasattr(langfuse_observability, "update_langfuse_observation")
    update_observation = langfuse_observability.update_langfuse_observation
    client = _Client()
    runtime = configure_langfuse(
        config=_config(),
        client_factory=lambda **_kwargs: client,
        provider_factory=lambda **_kwargs: _Provider(),
    )
    with langfuse_observation(as_type="tool", name="tool") as observation:
        update_observation(
            observation,
            input={"prompt": "send https://example.test/?token=secret"},
            output={"api_key": "secret", "result": "done"},
            metadata={"tool_result": "ordinary result", "auth": "Bearer secret"},
            status_message="completed using Bearer secret",
        )

    assert client.observations[0]["updates"] == [
        {
            "input": {
                "prompt": "send https://example.test/?token=[REDACTED]"
            },
            "output": {"result": "done"},
            "metadata": {
                "tool_result": "ordinary result",
                "auth": "[REDACTED]",
                "content_policy_applied": True,
            },
            "status_message": "completed using [REDACTED]",
        }
    ]
    runtime.shutdown()


def test_agent_run_propagates_only_hashed_identifiers_and_stable_trace_tag():
    client = _Client()
    runtime = configure_langfuse(
        config=_config(),
        client_factory=lambda **_kwargs: client,
        provider_factory=lambda **_kwargs: _Provider(),
        handler_factory=lambda **_kwargs: object(),
    )
    captured = {}

    @contextmanager
    def propagate(**kwargs):
        captured.update(kwargs)
        yield

    runtime.propagate_factory = propagate
    with langfuse_agent_run(
        business_trace_id="business-trace",
        session_key="session-secret",
        tenant_id="tenant-secret",
        user_id="user-secret",
        execution_segment="resume",
    ) as handle:
        assert isinstance(handle, LangfuseRunHandle)
        handle.update(output={"answer": "done", "api_key": "secret"})

    assert captured["session_id"] == hash_langfuse_identifier("session-secret")
    assert captured["user_id"] == hash_langfuse_identifier("user-secret")
    assert captured["tags"] == [
        f"business_trace_id:{hash_langfuse_identifier('business-trace')}"
    ]
    assert client.observations[0]["updates"] == [
        {
            "output": {"answer": "done"},
            "metadata": {"content_policy_applied": True},
        }
    ]
    assert "session-secret" not in str(captured)
    assert "user-secret" not in str(captured)
    runtime.shutdown()


def test_langfuse_runtime_failure_does_not_escape_into_agent_run():
    class FailingClient(_Client):
        def start_as_current_observation(self, **_kwargs):
            raise RuntimeError("sdk failed")

    runtime = configure_langfuse(
        config=_config(),
        client_factory=lambda **_kwargs: FailingClient(),
        provider_factory=lambda **_kwargs: _Provider(),
        handler_factory=lambda **_kwargs: object(),
    )

    with langfuse_agent_run(
        business_trace_id="business-trace",
        session_key="session-secret",
        tenant_id="tenant-secret",
        execution_segment="initial",
    ) as callbacks:
        assert callbacks == ()

    assert runtime is not None
    runtime.shutdown()


def test_agent_run_closes_root_observation_when_attribute_propagation_fails():
    client = _Client()
    runtime = configure_langfuse(
        config=_config(),
        client_factory=lambda **_kwargs: client,
        provider_factory=lambda **_kwargs: _Provider(),
        handler_factory=lambda **_kwargs: object(),
    )
    assert runtime is not None

    def fail_propagation(**_kwargs):
        raise RuntimeError("propagation failed")

    runtime.propagate_factory = fail_propagation

    with langfuse_agent_run(
        business_trace_id="business-trace",
        session_key="session-secret",
        tenant_id="tenant-secret",
        execution_segment="initial",
    ) as callbacks:
        assert callbacks == ()

    assert client.observation_exit_calls == 1
    runtime.shutdown()


def test_langfuse_runtime_shutdown_is_idempotent():
    client = _Client()
    provider = _Provider()
    runtime = configure_langfuse(
        config=_config(),
        client_factory=lambda **_kwargs: client,
        provider_factory=lambda **_kwargs: provider,
        handler_factory=lambda **_kwargs: object(),
    )

    assert runtime is not None
    runtime.shutdown()
    runtime.shutdown()

    assert provider.flush_calls == [5_000]
    assert client.shutdown_calls == 1
    assert provider.shutdown_calls == 1


def test_hash_langfuse_identifier_is_stable_and_domain_separated():
    first = hash_langfuse_identifier("session-secret")

    assert first == hash_langfuse_identifier("session-secret")
    assert first != "session-secret"
    assert len(first) == 64


def test_mask_langfuse_otel_spans_removes_content_but_preserves_usage():
    identifier = OtelSpanIdentifier(trace_id="0" * 32, span_id="1" * 16)
    params = MaskOtelSpansParams(
        spans={
            identifier: OtelSpanData(
                trace_id="0" * 32,
                span_id="1" * 16,
                parent_span_id=None,
                name="deepseek-generation",
                instrumentation_scope_name="langchain",
                instrumentation_scope_version=None,
                attributes={
                    "langfuse.observation.input": "secret prompt",
                    "langfuse.observation.output": "secret answer",
                    "gen_ai.prompt.0.content": "secret prompt",
                    "gen_ai.completion.0.content": "secret answer",
                    "gen_ai.output.messages.0.reasoning_content": "secret reasoning",
                    "gen_ai.usage.input_tokens": 12,
                    "gen_ai.usage.output_reasoning_tokens": 5,
                    "gen_ai.response.model": "deepseek-v4-flash",
                },
                resource_attributes={},
            )
        }
    )

    result = mask_langfuse_otel_spans(params=params)
    patch = result.span_patches[identifier]

    assert patch is not None
    assert set(patch.delete_attributes) == {
        "langfuse.observation.input",
        "langfuse.observation.output",
        "gen_ai.prompt.0.content",
        "gen_ai.completion.0.content",
        "gen_ai.output.messages.0.reasoning_content",
    }
    assert "gen_ai.usage.input_tokens" not in patch.delete_attributes
    assert "gen_ai.usage.output_reasoning_tokens" not in patch.delete_attributes
    assert "gen_ai.response.model" not in patch.delete_attributes


def test_mask_preserves_sanitized_langfuse_io_only_for_policy_managed_spans():
    managed = OtelSpanIdentifier(trace_id="0" * 32, span_id="1" * 16)
    unmanaged = OtelSpanIdentifier(trace_id="0" * 32, span_id="2" * 16)
    attributes = {
        "langfuse.observation.input": '{"prompt":"sanitized"}',
        "langfuse.observation.output": '{"answer":"sanitized"}',
        "langfuse.trace.input": '{"prompt":"sanitized"}',
        "langfuse.trace.output": '{"answer":"sanitized"}',
        "gen_ai.input.messages": '[{"content":"raw prompt"}]',
        "gen_ai.output.messages": '[{"content":"raw answer"}]',
        "langfuse.observation.metadata.arbitrary": "private metadata",
    }
    params = MaskOtelSpansParams(
        spans={
            managed: OtelSpanData(
                trace_id=managed.trace_id,
                span_id=managed.span_id,
                parent_span_id=None,
                name="managed",
                instrumentation_scope_name="bs-agent",
                instrumentation_scope_version=None,
                attributes={
                    **attributes,
                    "langfuse.observation.metadata.content_policy_applied": True,
                },
                resource_attributes={},
            ),
            unmanaged: OtelSpanData(
                trace_id=unmanaged.trace_id,
                span_id=unmanaged.span_id,
                parent_span_id=None,
                name="callback",
                instrumentation_scope_name="langchain",
                instrumentation_scope_version=None,
                attributes=attributes,
                resource_attributes={},
            ),
        }
    )

    result = mask_langfuse_otel_spans(params=params)
    managed_deleted = set(result.span_patches[managed].delete_attributes)
    unmanaged_deleted = set(result.span_patches[unmanaged].delete_attributes)

    assert not managed_deleted & {
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.trace.input",
        "langfuse.trace.output",
    }
    assert {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "langfuse.observation.metadata.arbitrary",
    } <= managed_deleted
    assert {
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.trace.input",
        "langfuse.trace.output",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
    } <= unmanaged_deleted


def test_mask_blocks_trace_io_callback_metadata_and_raw_errors():
    identifier = OtelSpanIdentifier(trace_id="0" * 32, span_id="1" * 16)
    blocked = {
        "langfuse.trace.input": "private prompt",
        "langfuse.trace.output": "private answer",
        "langfuse.observation.metadata.tool_result": '{"secret": "tool data"}',
        "langfuse.observation.metadata.arbitrary": "private content",
        "langfuse.trace.metadata": '{"prompt": "private prompt"}',
        "langfuse.observation.status_message": "Error with private tool arguments",
    }
    allowed = {
        "langfuse.observation.metadata.business_trace_id": "trace-smoke",
        "langfuse.observation.metadata.execution_segment": "initial",
        "langfuse.observation.metadata.reasoning_chars": 42,
        "langfuse.observation.metadata.ttft_ms": 12.5,
        "langfuse.observation.metadata.system_prompt_chars": 20,
        "langfuse.observation.metadata.content_chars": 10,
        "langfuse.observation.usage_details": '{"input":12,"output":8,"total":20}',
        "langfuse.observation.model.name": "deepseek-v4-flash",
    }
    params = MaskOtelSpansParams(
        spans={
            identifier: OtelSpanData(
                trace_id=identifier.trace_id,
                span_id=identifier.span_id,
                parent_span_id=None,
                name="smoke",
                instrumentation_scope_name="langfuse",
                instrumentation_scope_version=None,
                attributes={**blocked, **allowed},
                resource_attributes={},
            )
        }
    )
    result = mask_langfuse_otel_spans(params=params)
    deleted = set(result.span_patches[identifier].delete_attributes)
    assert set(blocked) <= deleted
    assert not set(allowed) & deleted


def test_mask_preserves_only_stable_error_codes():
    identifier = OtelSpanIdentifier(trace_id="0" * 32, span_id="1" * 16)
    params = MaskOtelSpansParams(
        spans={
            identifier: OtelSpanData(
                trace_id=identifier.trace_id,
                span_id=identifier.span_id,
                parent_span_id=None,
                name="smoke",
                instrumentation_scope_name="langfuse",
                instrumentation_scope_version=None,
                attributes={
                    "langfuse.observation.status_message": "llm_timeout:read:before_first_token"
                },
                resource_attributes={},
            )
        }
    )
    result = mask_langfuse_otel_spans(params=params)
    assert identifier not in result.span_patches


def test_metadata_only_suppresses_io_at_every_observation_boundary():
    client = _Client()
    runtime = configure_langfuse(
        config=_config(langfuse_content_mode="metadata_only"),
        client_factory=lambda **_kwargs: client,
        provider_factory=lambda **_kwargs: _Provider(),
    )
    try:
        with langfuse_agent_run(
            business_trace_id="trace", session_key="session", tenant_id="tenant",
            execution_segment="initial", input={"message": "private"},
        ) as handle:
            handle.update(input="private", output="private")
        with langfuse_generation(
            name="generation", model="deepseek", model_parameters={},
            system_prompt="private", user_prompt="private",
        ) as generation:
            update_langfuse_generation(generation, input="private", output="private")
        with langfuse_observation(as_type="tool", name="tool", input="private") as observation:
            langfuse_observability.update_langfuse_observation(
                observation, input="private", output="private"
            )
        for observation in client.observations:
            assert "input" not in observation
            assert "output" not in observation
            for update in observation.get("updates", []):
                assert "input" not in update
                assert "output" not in update
    finally:
        runtime.shutdown()


def test_real_generation_update_preserves_safe_latency_statistics_and_redacts_secrets():
    client = _Client()
    runtime = configure_langfuse(
        config=_config(), client_factory=lambda **_kwargs: client,
        provider_factory=lambda **_kwargs: _Provider(),
    )
    try:
        with langfuse_generation(name="generation", model="deepseek", model_parameters={}) as generation:
            update_langfuse_generation(
                generation,
                metadata={
                    "first_token_received": True,
                    "first_token_kind": "reasoning",
                    "reasoning_chars": 23,
                    "ttft_ms": 12.5,
                    "reasoning_content": "private reasoning",
                    "api_key": "private credential",
                },
            )
        metadata = client.observations[0]["updates"][0]["metadata"]
        assert metadata == {
            "first_token_received": True,
            "first_token_kind": "reasoning",
            "reasoning_chars": 23,
            "ttft_ms": 12.5,
            "content_policy_applied": True,
        }
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("helper", ["generation", "observation", "handle"])
def test_observation_sanitization_failure_does_not_escape(helper):
    class BrokenPolicy:
        mode = "local_content"

        def sanitize(self, _value):
            raise ValueError("sanitizer failed")

    observation = SimpleNamespace(update=lambda **_kwargs: None)
    runtime = configure_langfuse(
        config=_config(), client_factory=lambda **_kwargs: _Client(),
        provider_factory=lambda **_kwargs: _Provider(),
    )
    runtime.content_policy = BrokenPolicy()
    try:
        if helper == "handle":
            LangfuseRunHandle(observation=observation, content_policy=runtime.content_policy).update(output="value")
        else:
            update = (
                update_langfuse_generation if helper == "generation"
                else langfuse_observability.update_langfuse_observation
            )
            update(observation, output="value")
    finally:
        runtime.shutdown()


def test_metadata_only_export_mask_rejects_managed_span_io():
    client_kwargs = {}
    runtime = configure_langfuse(
        config=_config(langfuse_content_mode="metadata_only"),
        client_factory=lambda **kwargs: client_kwargs.update(kwargs) or _Client(),
        provider_factory=lambda **_kwargs: _Provider(),
    )
    identifier = OtelSpanIdentifier(trace_id="0" * 32, span_id="1" * 16)
    io_keys = {
        "langfuse.observation.input", "langfuse.observation.output",
        "langfuse.trace.input", "langfuse.trace.output",
    }
    params = MaskOtelSpansParams(spans={identifier: OtelSpanData(
        trace_id=identifier.trace_id, span_id=identifier.span_id,
        parent_span_id=None, name="managed", instrumentation_scope_name="bs-agent",
        instrumentation_scope_version=None, resource_attributes={},
        attributes={
            **dict.fromkeys(io_keys, "private"),
            "langfuse.observation.metadata.content_policy_applied": True,
        },
    )})
    runtime.shutdown()
    result = client_kwargs["mask_otel_spans"](params=params)
    assert io_keys <= set(result.span_patches[identifier].delete_attributes)


def test_agent_run_propagation_cancellation_closes_root_and_propagates():
    import asyncio

    client = _Client()
    runtime = configure_langfuse(
        config=_config(), client_factory=lambda **_kwargs: client,
        provider_factory=lambda **_kwargs: _Provider(),
        handler_factory=lambda **_kwargs: object(),
    )

    def cancel(**_kwargs):
        raise asyncio.CancelledError()

    runtime.propagate_factory = cancel
    try:
        with pytest.raises(asyncio.CancelledError):
            with langfuse_agent_run(
                business_trace_id="trace", session_key="session", tenant_id="tenant",
                execution_segment="initial",
            ):
                pytest.fail("取消后不能继续业务")
        assert client.observation_exit_calls == 1
    finally:
        runtime.shutdown()
