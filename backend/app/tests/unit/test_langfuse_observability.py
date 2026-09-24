from contextlib import contextmanager
from types import SimpleNamespace

from app.operations.langfuse_observability import (
    configure_langfuse,
    hash_langfuse_identifier,
    langfuse_agent_run,
    langfuse_generation,
    mask_langfuse_otel_spans,
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
        "langfuse_base_url": "https://jp.cloud.langfuse.com",
        "langfuse_sample_rate": 1.0,
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

        @contextmanager
        def observation():
            try:
                yield SimpleNamespace(update=lambda **_kwargs: None)
            finally:
                self.observation_exit_calls += 1

        return observation()


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
    assert client_calls[0]["base_url"] == "https://jp.cloud.langfuse.com"
    assert client_calls[0]["environment"] == "test"
    assert client_calls[0]["sample_rate"] == 1.0
    assert client_calls[0]["tracer_provider"] is provider
    assert client_calls[0]["mask_otel_spans"] is mask_langfuse_otel_spans

    with langfuse_agent_run(
        business_trace_id="business-trace",
        session_key="session-secret",
        tenant_id="tenant-secret",
        execution_segment="initial",
    ) as callbacks:
        assert len(callbacks) == 1

    assert handler_calls == [{"public_key": "pk-test"}]
    assert client.observations[0]["trace_context"] == {
        "trace_id": "trace:business-trace"
    }
    assert "input" not in client.observations[0]
    assert "output" not in client.observations[0]
    assert client.observations[0]["metadata"] == {
        "business_trace_id": "business-trace",
        "execution_segment": "initial",
        "tenant_hash": hash_langfuse_identifier("tenant-secret"),
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
    ) as generation:
        assert generation is not None

    observation = client.observations[0]
    assert observation["as_type"] == "generation"
    assert observation["model"] == "deepseek-v4-flash"
    assert "input" not in observation
    assert "output" not in observation
    assert runtime is not None
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
