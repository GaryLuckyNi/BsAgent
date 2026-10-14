from types import SimpleNamespace

from app.core.config import Settings
from app.operations.langfuse_observability import (
    configure_langfuse,
    hash_langfuse_identifier,
    langfuse_agent_run,
    langfuse_generation,
    update_langfuse_generation,
)


class _Observation:
    def __init__(self) -> None:
        self.id = "root-observation"
        self.closed = False
        self.updates: list[dict] = []

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        self.end()

    def end(self):
        self.closed = True


class _Client:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.observations: list[dict] = []
        self.shutdown_count = 0

    def create_trace_id(self, *, seed: str) -> str:
        return f"trace-{seed}"

    def start_as_current_observation(self, **kwargs):
        self.observations.append(kwargs)
        return _Observation()

    def start_observation(self, **kwargs):
        self.observations.append(kwargs)
        observation = _Observation()
        self.started_observation = observation
        return observation

    def shutdown(self) -> None:
        self.shutdown_count += 1


class _Provider:
    def __init__(self) -> None:
        self.flush_count = 0
        self.shutdown_count = 0

    def force_flush(self, *, timeout_millis: int) -> None:
        assert timeout_millis == 5_000
        self.flush_count += 1

    def shutdown(self) -> None:
        self.shutdown_count += 1


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        langfuse_enabled=True,
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        langfuse_base_url="http://localhost:3000",
        **overrides,
    )


def test_configure_langfuse_uses_local_endpoint_and_shutdown_is_idempotent():
    provider = _Provider()
    created: list[_Client] = []

    def client_factory(**kwargs):
        client = _Client(**kwargs)
        created.append(client)
        return client

    runtime = configure_langfuse(
        config=_settings(),
        client_factory=client_factory,
        provider_factory=lambda **_kwargs: provider,
        handler_factory=lambda **_kwargs: "handler",
    )

    assert runtime is not None
    assert created[0].kwargs["base_url"] == "http://localhost:3000"
    assert created[0].kwargs["sample_rate"] == 1.0
    runtime.shutdown()
    runtime.shutdown()
    assert provider.flush_count == 1
    assert provider.shutdown_count == 1
    assert created[0].shutdown_count == 1


def test_agent_run_uses_explicit_trace_context_without_cross_yield_context():
    provider = _Provider()
    client = _Client()
    handler_options: list[dict] = []

    def handler_factory(**kwargs):
        handler_options.append(kwargs)
        return "handler"

    runtime = configure_langfuse(
        config=_settings(),
        client_factory=lambda **kwargs: (client.kwargs.update(kwargs) or client),
        provider_factory=lambda **_kwargs: provider,
        handler_factory=handler_factory,
    )
    assert runtime is not None

    with langfuse_agent_run(
        business_trace_id="business-1",
        session_key="session-1",
        tenant_id="tenant-1",
        execution_segment="initial",
        input_data={"message": "分析银之杰", "api_key": "secret"},
    ) as run:
        assert run.callbacks == ("handler",)
        run.set_output({"content": "分析完成"})

    assert client.observations[0]["trace_context"] == {"trace_id": "trace-business-1"}
    assert client.observations[0]["metadata"]["tenant_hash"] == hash_langfuse_identifier(
        "tenant-1"
    )
    assert client.observations[0]["metadata"]["session_hash"] == hash_langfuse_identifier(
        "session-1"
    )
    assert client.observations[0]["input"] == {
        "message": "分析银之杰",
        "api_key": "***",
    }
    assert client.started_observation.updates == [{"output": {"content": "分析完成"}}]
    assert handler_options == [
        {
            "public_key": "pk-test",
            "trace_context": {
                "trace_id": "trace-business-1",
                "parent_span_id": "root-observation",
            },
        }
    ]
    assert client.started_observation.closed is True
    runtime.shutdown()


def _mask_deleted_attributes(*, mode: str, capture_reasoning: bool = False) -> set[str]:
    client = _Client()
    runtime = configure_langfuse(
        config=_settings(
            langfuse_content_mode=mode,
            langfuse_capture_reasoning=capture_reasoning,
        ),
        client_factory=lambda **kwargs: (client.kwargs.update(kwargs) or client),
        provider_factory=lambda **_kwargs: _Provider(),
        handler_factory=lambda **_kwargs: "handler",
    )
    assert runtime is not None
    mask = client.kwargs["mask_otel_spans"]
    result = mask(
        params=SimpleNamespace(
            spans={
                "span": SimpleNamespace(
                    attributes={
                        "langfuse.observation.input": "input",
                        "langfuse.observation.output": "output",
                        "gen_ai.prompt.0.content": "prompt",
                        "gen_ai.completion.0.content": "completion",
                        "gen_ai.reasoning": "reasoning",
                    }
                )
            }
        )
    )
    runtime.shutdown()
    patch = result.span_patches.get("span")
    return set(patch.delete_attributes if patch else ())


def test_local_content_keeps_input_output_but_removes_reasoning():
    deleted = _mask_deleted_attributes(mode="local_content")

    assert "langfuse.observation.input" not in deleted
    assert "langfuse.observation.output" not in deleted
    assert "gen_ai.prompt.0.content" not in deleted
    assert "gen_ai.completion.0.content" not in deleted
    assert "gen_ai.reasoning" in deleted


def test_metadata_only_removes_content_and_reasoning():
    deleted = _mask_deleted_attributes(mode="metadata_only")

    assert "langfuse.observation.input" in deleted
    assert "langfuse.observation.output" in deleted
    assert "gen_ai.prompt.0.content" in deleted
    assert "gen_ai.completion.0.content" in deleted
    assert "gen_ai.reasoning" in deleted


def test_local_debug_can_keep_reasoning_when_explicitly_enabled():
    deleted = _mask_deleted_attributes(mode="local_debug", capture_reasoning=True)

    assert "langfuse.observation.input" not in deleted
    assert "langfuse.observation.output" not in deleted
    assert "gen_ai.reasoning" not in deleted


def test_generation_records_redacted_input_and_output_in_local_content_mode():
    client = _Client()
    runtime = configure_langfuse(
        config=_settings(langfuse_content_mode="local_content"),
        client_factory=lambda **kwargs: (client.kwargs.update(kwargs) or client),
        provider_factory=lambda **_kwargs: _Provider(),
        handler_factory=lambda **_kwargs: "handler",
    )
    assert runtime is not None

    with langfuse_generation(
        name="generation",
        model="model",
        model_parameters={"max_tokens": 10},
        input_data=[{"role": "user", "content": "hello", "token": "secret"}],
    ) as generation:
        update_langfuse_generation(generation, output={"content": "world"})

    assert client.observations[0]["input"] == [
        {"role": "user", "content": "hello", "token": "***"}
    ]
    assert generation.updates == [{"output": {"content": "world"}}]
    assert generation.closed is True
    runtime.shutdown()
