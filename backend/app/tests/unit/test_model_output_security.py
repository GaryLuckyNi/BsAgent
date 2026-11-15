"""模型结果与外发观测使用合成canary和内存transport。"""

import asyncio
import logging
from io import StringIO

import httpx
import pytest
from app.llm.deepseek import DeepSeekClient, LlmProviderError
from app.llm.factory import invoke_llm_completion
from app.llm.schemas import LlmCompletionResult, LlmStreamDelta
from app.operations.langfuse_content import sanitize_content
from app.operations.observability import configure_logging

CANARY = "MODEL_OUTPUT_CREDENTIAL_CANARY"
TEXT = 'Provider: {"api_key":"MODEL_OUTPUT_CREDENTIAL_CANARY"}'


@pytest.mark.parametrize("mode", ["complete", "stream", "sync_stream"])
@pytest.mark.parametrize("failure", ["http", "timeout", "invalid"])
def test_provider_failures_never_expose_response_body_or_raw_exception_chain(mode, failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(TEXT, request=request)
        return (
            httpx.Response(500, text=TEXT) if failure == "http" else httpx.Response(200, text=TEXT)
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://model.invalid"
        ) as client:
            with httpx.Client(
                transport=httpx.MockTransport(respond), base_url="https://model.invalid"
            ) as sync:
                model = DeepSeekClient(
                    api_key="synthetic-auth",
                    model="test",
                    http_client=client,
                    sync_http_client=sync,
                )
                if mode == "complete":
                    return await model.complete(system_prompt="公开研究", user_prompt="公开查询")
                if mode == "stream":
                    return [
                        item
                        async for item in model.stream_complete_events(
                            system_prompt="公开研究", user_prompt="公开查询"
                        )
                    ]
                return model.complete_streamed_sync(
                    system_prompt="公开研究", user_prompt="公开查询", on_delta=lambda _: None
                )

    with pytest.raises(LlmProviderError) as caught:
        asyncio.run(run())
    assert CANARY not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert caught.value.code in {"llm_http_error", "llm_timeout", "llm_invalid_response"}


@pytest.mark.parametrize(
    "source",
    [
        TEXT,
        "Cookie: sid=" + CANARY,
        "-----BEGIN PRIVATE KEY-----\n" + CANARY + "\n-----END PRIVATE KEY-----",
    ],
)
def test_factory_buffers_all_fragments_before_releasing_checked_result(source):
    received = []

    class Client:
        def complete_streamed_sync(self, **kwargs):
            for char in source:
                kwargs["on_delta"](LlmStreamDelta(kind="content", delta=char))
                assert received == []
            return LlmCompletionResult(content=source, model="test", provider="test")

    result = invoke_llm_completion(
        Client(), system_prompt="研究", user_prompt="公开查询", on_delta=received.append
    )
    assert received
    assert CANARY not in result.content
    assert CANARY not in str(received)


def test_failed_stream_does_not_release_any_partial_body():
    received = []

    class Client:
        def complete_streamed_sync(self, **kwargs):
            kwargs["on_delta"](LlmStreamDelta(kind="content", delta=TEXT))
            raise httpx.ReadTimeout("synthetic-timeout")

    with pytest.raises(LlmProviderError):
        invoke_llm_completion(
            Client(), system_prompt="研究", user_prompt="公开查询", on_delta=received.append
        )
    assert received == []


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_managed_logging_cleans_message_and_exception_without_mutating_record(fmt):
    stream = StringIO()
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        configure_logging(log_format=fmt, level="INFO", stream=stream)
        try:
            raise RuntimeError(TEXT)
        except RuntimeError:
            logging.getLogger("bs_agent.security-test").exception("provider failed %s", TEXT)
    finally:
        root.handlers, root.level = handlers, level
    assert CANARY not in stream.getvalue()


@pytest.mark.parametrize(
    "value",
    [
        TEXT,
        "Cookie: sid=" + CANARY,
        "-----BEGIN PRIVATE KEY-----\n" + CANARY + "\n-----END PRIVATE KEY-----",
    ],
)
def test_langfuse_text_uses_shared_credential_floor(value):
    assert CANARY not in str(sanitize_content({"output": value}))


@pytest.mark.parametrize("mode", ["invoke", "ainvoke", "stream", "astream"])
def test_native_model_result_is_checked_after_complete_and_source_is_unchanged(mode):
    from app.llm.input_guard import ProtectedModel
    from langchain_core.messages import AIMessage, AIMessageChunk
    from langchain_core.runnables import Runnable

    received = []
    source = AIMessage(content=TEXT, id="public-response")

    class Native(Runnable):
        def invoke(self, *args, **kwargs):
            return source

        def stream(self, *args, **kwargs):
            for char in TEXT:
                assert not received
                yield AIMessageChunk(content=char, id="public-response")

        async def astream(self, *args, **kwargs):
            for chunk in self.stream():
                yield chunk

    model = ProtectedModel(Native())

    async def run():
        if mode == "ainvoke":
            received.append(await model.ainvoke("公开查询"))
        else:
            async for chunk in model.astream("公开查询"):
                received.append(chunk)

    if mode == "invoke":
        received.append(model.invoke("公开查询"))
    elif mode == "stream":
        received.extend(model.stream("公开查询"))
    else:
        asyncio.run(run())
    assert CANARY not in str(received)
    assert all(item.id == "public-response" for item in received)
    assert source.content == TEXT


def test_model_does_not_rewrite_sensitive_executable_arguments():
    from app.llm.input_guard import ProtectedModel
    from app.security.model_output import ModelOutputRejected
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda

    response = AIMessage(
        content="", tool_calls=[{"id": "call-1", "name": "echo", "args": {"api_key": CANARY}}]
    )
    with pytest.raises(ModelOutputRejected):
        ProtectedModel(RunnableLambda(lambda _: response)).invoke("公开查询")
    assert response.tool_calls[0]["args"]["api_key"] == CANARY


def test_public_tool_arguments_preserve_exact_json_protocol_text():
    from app.security.model_output import protect_message_response
    from langchain_core.messages import AIMessageChunk
    args = '{"symbol":"600519.SH","context":"{\\"fact\\":1}"}'
    message = AIMessageChunk(content="", tool_call_chunks=[{"index":0, "id":"call-1", "name":"lookup", "args":args}],
                             additional_kwargs={"tool_calls":[{"id":"call-1", "type":"function", "function":{"name":"lookup", "arguments":args}}]})
    result = protect_message_response(message)
    assert result.tool_call_chunks == message.tool_call_chunks
    assert result.tool_calls == message.tool_calls
    assert result.additional_kwargs == message.additional_kwargs


def test_trace_memory_and_persistence_use_safe_copies():
    from app.harness.persistence import TraceRunIdentity, _event_values
    from app.harness.tracing import InMemoryTraceStore, TraceArtifact, TraceEvent

    event = TraceEvent(
        trace_id="trace-test",
        node_name="node",
        status="error",
        latency_ms=1,
        error=TEXT,
        metadata={"detail": TEXT},
    )
    store = InMemoryTraceStore()
    store.record_event(event)
    artifact = TraceArtifact(
        trace_id="trace-test",
        node_name="node",
        artifact_type="output_summary",
        payload={"detail": TEXT},
    )
    ref = store.save_artifact(artifact)
    assert CANARY not in str(store.list_events("trace-test"))
    assert CANARY not in str(store.get_artifact(ref))
    assert event.error == TEXT and artifact.payload["detail"] == TEXT
    run = TraceRunIdentity(
        trace_id="trace-test",
        run_id="run-test",
        session_key="session-test",
        user_id="user-test",
        tenant_id="tenant-test",
    )
    assert CANARY not in str(_event_values(run=run, event=event, capture_level="detailed"))


def test_actual_span_export_protects_all_text_and_preserves_identity():
    from app.operations.safe_exporter import SafeSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import Status, StatusCode

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(SafeSpanExporter(exporter)))
    with provider.get_tracer("security-test").start_as_current_span("response") as span:
        context = span.get_span_context()
        span.set_attribute("response", TEXT)
        span.record_exception(RuntimeError(TEXT))
        span.set_status(Status(StatusCode.ERROR, TEXT))
    (exported,) = exporter.get_finished_spans()
    assert CANARY not in exported.to_json()
    assert exported.context == context
    assert exported.status.status_code == StatusCode.ERROR
    provider.shutdown()


@pytest.mark.parametrize("mode", ["events", "log"])
def test_runnable_diagnostics_buffer_fragments_and_only_publish_checked_final(mode):
    from app.llm.input_guard import ProtectedModel
    from langchain_core.messages import AIMessage, AIMessageChunk
    from langchain_core.runnables import RunnableLambda
    from langchain_core.tracers.log_stream import RunLogPatch

    received = []

    class Native(RunnableLambda):
        async def astream_events(self, *args, **kwargs):
            for char in TEXT:
                assert not received
                yield {
                    "event": "on_chat_model_stream",
                    "data": {"chunk": AIMessageChunk(content=char)},
                }
            yield {"event": "on_chat_model_end", "data": {"output": AIMessage(content=TEXT)}}

        async def astream_log(self, *args, **kwargs):
            yield RunLogPatch(
                {
                    "op": "replace",
                    "path": "",
                    "value": {"streamed_output": [], "final_output": None},
                }
            )
            for char in TEXT:
                assert not received
                yield RunLogPatch({"op": "add", "path": "/streamed_output/-", "value": char})
            yield RunLogPatch(
                {"op": "replace", "path": "/final_output", "value": AIMessage(content=TEXT)}
            )

    async def run():
        model = ProtectedModel(Native(lambda _: "unused"))
        events = (
            model.astream_events("公开查询") if mode == "events" else model.astream_log("公开查询")
        )
        async for event in events:
            received.append(event)

    asyncio.run(run())
    assert received and CANARY not in str(received)


def test_real_langfuse_sdk_drops_unmaskable_provider_exception_span():
    from uuid import uuid4

    from app.core.config import Settings
    from app.operations.langfuse_observability import configure_langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from langfuse import Langfuse

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    ) as transport:
        runtime = configure_langfuse(
            config=Settings(
                _env_file=None,
                langfuse_enabled=True,
                langfuse_public_key=f"pk-test-{uuid4().hex}",
                langfuse_secret_key="synthetic",
            ),
            provider_factory=lambda **_: provider,
            client_factory=lambda **kwargs: Langfuse(
                **kwargs, span_exporter=exporter, httpx_client=transport
            ),
        )
        try:
            with pytest.raises(RuntimeError), runtime.client.start_as_current_observation(name="provider-error"):
                raise RuntimeError(TEXT)
            with runtime.client.start_as_current_observation(name="safe-result", output=TEXT):
                pass
            runtime.client.flush()
            provider.force_flush()
            spans = exporter.get_finished_spans()
            assert "provider-error" not in {span.name for span in spans}
            assert "safe-result" in {span.name for span in spans}
            assert all(CANARY not in span.to_json() for span in spans)
        finally:
            runtime.shutdown()


@pytest.mark.parametrize("payload", ["data: []\n\n", "data: null\n\n", "data: invalid-json\n\n"])
def test_malformed_stream_packets_use_fixed_provider_error_without_chain(payload):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=payload)),
        base_url="https://model.invalid",
    ) as transport:
        model = DeepSeekClient(api_key="synthetic", model="test", sync_http_client=transport)
        with pytest.raises(LlmProviderError) as caught:
            model.complete_streamed_sync(
                system_prompt="研究", user_prompt="公开查询", on_delta=lambda _: None
            )
    assert caught.value.code == "llm_invalid_response"
    assert caught.value.__context__ is None


def test_completion_serialization_failure_has_no_raw_exception_chain():
    from app.security.model_output import ModelOutputUnavailable, protect_completion

    class Unsafe:
        def __repr__(self):
            return TEXT

    response = LlmCompletionResult(
        content="公开事实", model="test", provider="test", usage={"unknown": Unsafe()}
    )
    with pytest.raises(ModelOutputUnavailable) as caught:
        protect_completion(response)
    assert caught.value.__context__ is None
    assert CANARY not in str(caught.value)


def test_direct_stream_never_emits_body_if_final_metadata_check_fails(monkeypatch):
    from functools import partial

    import app.llm.deepseek as module
    from app.security.http_response import HttpResponseLimits
    from app.security.model_output import ModelOutputRejected

    # 单独验证最终元数据检查：提高本测试网络行预算，避免先被更早的读取层拒绝。
    monkeypatch.setattr(module, "bounded_stream", partial(
        module.bounded_stream, limits=HttpResponseLimits(max_line_bytes=2_000_000),
    ))
    received = []
    # model 元数据也在最终检查范围；不能先发布正文再发现结果过大。
    body = (
        "data: "
        + __import__("json").dumps(
            {"model": "x" * 1_000_001, "choices": [{"delta": {"content": "公开事实"}}]}
        )
        + "\n\ndata: [DONE]\n\n"
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)),
        base_url="https://model.invalid",
    ) as transport:
        model = DeepSeekClient(api_key="synthetic", model="test", sync_http_client=transport)
        with pytest.raises(ModelOutputRejected):
            model.complete_streamed_sync(
                system_prompt="研究", user_prompt="公开查询", on_delta=received.append
            )
    assert received == []


@pytest.mark.parametrize("kind", ["json", "text"])
def test_managed_formatter_does_not_mutate_shared_log_record(kind):
    from app.operations.observability import SafeJsonFormatter, SafeTextFormatter

    record = logging.LogRecord(
        "security-test", logging.ERROR, __file__, 1, "response %s", (TEXT,), None
    )
    before = record.__dict__.copy()
    formatter = SafeJsonFormatter() if kind == "json" else SafeTextFormatter()
    assert CANARY not in formatter.format(record)
    assert record.__dict__ == before


def test_span_envelope_tracestate_is_checked_for_context_parent_and_links():
    from app.operations.safe_exporter import safe_span, should_export_langfuse_span
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Link, SpanContext, TraceFlags, TraceState

    context = SpanContext(
        1,
        2,
        True,
        TraceFlags(1),
        TraceState([("vendor", "Bearer TRACESTATE_CREDENTIAL_CANARY"), ("safe", "public")]),
    )
    span = ReadableSpan(
        name="test", context=context, parent=context, resource=Resource({}), links=[Link(context)]
    )
    exported = safe_span(span)
    assert "TRACESTATE_CREDENTIAL_CANARY" not in exported.to_json()
    assert exported.context.trace_id == context.trace_id
    assert exported.context.span_id == context.span_id
    assert exported.parent.trace_flags == context.trace_flags
    assert exported.links[0].context.trace_state.get("safe") == "public"
    assert context.trace_state.get("vendor") == "Bearer TRACESTATE_CREDENTIAL_CANARY"
    assert not should_export_langfuse_span(span)


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_broken_log_format_does_not_dump_raw_arguments_to_stderr(fmt, capsys):
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    stream = StringIO()
    try:
        root.handlers = []  # 本测试只验收受管 handler，不经 pytest 的独立 handler。
        configure_logging(log_format=fmt, level="INFO", stream=stream)
        logging.getLogger("format-test").error("provider status %d", TEXT)
    finally:
        root.handlers, root.level = handlers, level
    assert CANARY not in capsys.readouterr().err
    assert stream.getvalue()


def test_langfuse_allows_public_tuple_resource_attributes():
    from app.operations.safe_exporter import should_export_langfuse_span
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan

    span = ReadableSpan(
        name="safe", resource=Resource({"process.command_args": ("python", "app.py")})
    )
    assert should_export_langfuse_span(span)


@pytest.mark.parametrize("mode", ["invoke", "stream", "ainvoke", "astream"])
@pytest.mark.parametrize("config_kind", ["outer", "model_owned", "explicit", "inner_bound"])
def test_real_sdk_callbacks_receive_only_checked_final_output(mode, config_kind):
    import json

    from app.llm.input_guard import ProtectedModel
    from langchain_core.callbacks import AsyncCallbackHandler, BaseCallbackHandler
    from langchain_openai import ChatOpenAI

    captured = []

    class Callback(BaseCallbackHandler):
        def on_llm_new_token(self, token, **kwargs):
            captured.append(token)

        def on_llm_end(self, response, **kwargs):
            captured.append(str(response))

    class AsyncCallback(AsyncCallbackHandler):
        async def on_llm_new_token(self, token, **kwargs):
            captured.append(token)

        async def on_llm_end(self, response, **kwargs):
            captured.append(str(response))

    handler = AsyncCallback() if mode.startswith("a") else Callback()

    def respond(request):
        payload = json.loads(request.content)
        if payload.get("stream"):
            text = "".join(
                "data: "
                + json.dumps(
                    {"choices": [{"index": 0, "delta": {"content": c}, "finish_reason": None}]}
                )
                + "\n\n"
                for c in TEXT
            )
            text += (
                "data: "
                + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                + "\n\ndata: [DONE]\n\n"
            )
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "id": "response",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": TEXT},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    def configured_model(sync_http, async_http):
        raw = ChatOpenAI(
            api_key="synthetic",
            model="test",
            http_client=sync_http,
            http_async_client=async_http,
            max_retries=0,
            callbacks=[handler] if config_kind == "model_owned" else None,
        )
        if config_kind == "inner_bound":
            raw = raw.with_config(callbacks=[handler])
        model = ProtectedModel(raw)
        return model.with_config(callbacks=[handler]) if config_kind == "outer" else model

    call_config = {"callbacks": [handler]} if config_kind == "explicit" else None
    if mode.startswith("a"):

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
                configured = configured_model(None, transport)
                if mode == "ainvoke":
                    return await configured.ainvoke("公开查询", config=call_config)
                return [item async for item in configured.astream("公开查询", config=call_config)]

        result = asyncio.run(run())
    else:
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            configured = configured_model(transport, None)
            result = (
                configured.invoke("公开查询", config=call_config)
                if mode == "invoke"
                else list(configured.stream("公开查询", config=call_config))
            )
    assert captured and CANARY not in str(captured)
    assert CANARY not in str(result)


@pytest.mark.parametrize("async_mode", [False, True])
def test_sdk_failed_stream_callback_never_receives_partial_result_or_raw_error(async_mode):
    import json

    from app.llm.input_guard import ProtectedModel
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_openai import ChatOpenAI

    captured = []

    class Callback(BaseCallbackHandler):
        def on_llm_new_token(self, token, **kwargs):
            captured.append(("partial", token))

        def on_llm_error(self, error, **kwargs):
            captured.append(("error", str(error), kwargs))

        def on_llm_end(self, response, **kwargs):
            captured.append(("end", str(response)))

    packet = (
        "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": TEXT}}]}) + "\n\n"
    ).encode()

    class Stream(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self):
            yield packet
            raise httpx.ReadTimeout(TEXT)

        async def __aiter__(self):
            yield packet
            raise httpx.ReadTimeout(TEXT)

    def respond(_):
        return httpx.Response(200, stream=Stream(), headers={"content-type": "text/event-stream"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            model = ProtectedModel(
                ChatOpenAI(
                    api_key="synthetic",
                    model="test",
                    http_async_client=transport,
                    max_retries=0,
                    callbacks=[Callback()],
                )
            )
            return [chunk async for chunk in model.astream("公开查询")]

    with pytest.raises(LlmProviderError) as caught:
        if async_mode:
            asyncio.run(run())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                model = ProtectedModel(
                    ChatOpenAI(
                        api_key="synthetic",
                        model="test",
                        http_client=transport,
                        max_retries=0,
                        callbacks=[Callback()],
                    )
                )
                list(model.stream("公开查询"))
    assert caught.value.__context__ is None
    assert captured and all(item[0] == "error" for item in captured)
    assert CANARY not in str(captured)
