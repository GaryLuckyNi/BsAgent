"""实际 httpx 流在解析前有界读取；不用预缓冲 Mock 响应证明网络上限。"""

import asyncio
import gzip
import json
import traceback
import zlib

import httpx
import pytest
from app.security.http_response import (
    HttpResponseBudgetError,
    HttpResponseLimits,
    async_bounded_request,
    async_bounded_stream,
    bounded_request,
    bounded_stream,
)


_UPSTREAM_MARKER = b"RT_HTTP_BODY_DO_NOT_EXPOSE_71A6"


class SyncBytes(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


class AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def raw_deflate(data):
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def request(mode, chunks, *, headers=None, status=200, limits=None):
    limits = limits or HttpResponseLimits(max_wire_bytes=64, max_decoded_bytes=64)
    stream = (SyncBytes if mode == "sync" else AsyncBytes)(chunks)
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(status, headers=headers, stream=stream)

    if mode == "sync":
        with httpx.Client(
            base_url="https://upstream.test",
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:

            def run():
                return bounded_request(client, "POST", "/read", limits=limits, timeout_seconds=1)

            try:
                result = run()
            except (HttpResponseBudgetError, httpx.HTTPError) as error:
                return error, stream, seen
    else:

        async def run():
            async with httpx.AsyncClient(
                base_url="https://upstream.test",
                transport=httpx.MockTransport(handler),
                follow_redirects=True,
            ) as client:
                return await async_bounded_request(
                    client, "POST", "/read", limits=limits, timeout_seconds=1
                )

        try:
            result = asyncio.run(run())
        except (HttpResponseBudgetError, httpx.HTTPError) as error:
            return error, stream, seen
    return result, stream, seen


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
def test_wire_bytes_counted_without_trusting_content_length(mode, headers):
    result, stream, _ = request(mode, [b"x" * 32] * 50, headers=headers)
    assert isinstance(result, HttpResponseBudgetError)
    assert result.code == "http_response_wire_limit"
    assert stream.reads == 3 and stream.closed
    assert result.__cause__ is None and result.__context__ is None


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("status", [302, 404, 429, 500])
def test_error_or_redirect_body_is_never_read_or_followed(mode, status):
    result, stream, seen = request(
        mode, [b"PRIVATE_BODY" * 1000], status=status, headers={"Location": "https://other.test"}
    )
    assert result.status_code == status and result.content == b""
    assert stream.reads == 0 and stream.closed and len(seen) == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    "encoding,compress",
    [("gzip", gzip.compress), ("deflate", zlib.compress), ("deflate", raw_deflate)],
)
def test_compressed_bomb_is_rejected_before_unbounded_decoding(mode, encoding, compress):
    result, stream, _ = request(
        mode,
        [compress(b"x" * 100_000)],
        headers={"Content-Encoding": encoding},
        limits=HttpResponseLimits(max_wire_bytes=1024, max_decoded_bytes=64),
    )
    assert isinstance(result, HttpResponseBudgetError)
    assert result.code == "http_response_decoded_limit" and stream.closed


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    "encoding,compress",
    [
        ("identity", lambda b: b),
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
        ("deflate", raw_deflate),
    ],
)
def test_normal_utf8_json_and_compression_are_preserved(mode, encoding, compress):
    data = {"text": "公开正常中文"}
    raw = compress(json.dumps(data, ensure_ascii=False).encode())
    result, stream, seen = request(
        mode, [raw[i : i + 1] for i in range(len(raw))], headers={"Content-Encoding": encoding}
    )
    assert result.json() == data and stream.closed
    assert seen[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    "headers,chunks",
    [
        ({"Content-Length": "10000"}, [_UPSTREAM_MARKER]),
        ({"Content-Length": "-1"}, [_UPSTREAM_MARKER]),
        ({"Content-Encoding": "br"}, [_UPSTREAM_MARKER]),
        ({"Content-Encoding": "gzip, gzip"}, [_UPSTREAM_MARKER]),
        ({"Content-Encoding": "gzip"}, [b"invalid-" + _UPSTREAM_MARKER]),
        ({"Content-Encoding": "gzip"}, [gzip.compress(b"ok")[:-3]]),
        ({"Content-Encoding": "gzip"}, [gzip.compress(b"ok") + b"trailing-" + _UPSTREAM_MARKER]),
    ],
)
def test_invalid_length_encoding_and_compression_fail_with_fixed_error(mode, headers, chunks):
    result, stream, _ = request(mode, chunks, headers=headers)
    assert isinstance(result, HttpResponseBudgetError) and stream.closed
    assert _UPSTREAM_MARKER.decode() not in "".join(traceback.format_exception(result))
    assert result.__cause__ is None and result.__context__ is None


def test_async_absolute_deadline_stops_continuous_small_reads_and_closes():
    class Slow(AsyncBytes):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.002)
                self.reads += 1
                yield b"x"

    stream = Slow([])

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as client:
            await async_bounded_request(
                client, "GET", "https://upstream.test", timeout_seconds=0.025
            )

    with pytest.raises(HttpResponseBudgetError) as caught:
        asyncio.run(run())
    assert caught.value.code == "http_response_deadline"
    assert stream.closed and stream.reads < 100
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_sse_line_limit_applies_before_newline_or_json_parse(mode):
    limits = HttpResponseLimits(max_line_bytes=32)
    stream = (SyncBytes if mode == "sync" else AsyncBytes)(
        [b"data: " + b"x" * 20, b"y" * 20, b"z" * 20]
    )
    if mode == "sync":
        with (
            httpx.Client(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
            ) as client,
            pytest.raises(HttpResponseBudgetError) as caught,
            bounded_stream(
                client, "GET", "https://upstream.test", limits=limits, timeout_seconds=1
            ) as response,
        ):
            list(response.iter_lines())
    else:

        async def run():
            async with (
                httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
                ) as client,
                async_bounded_stream(
                    client, "GET", "https://upstream.test", limits=limits, timeout_seconds=1
                ) as response,
            ):
                return [line async for line in response.aiter_lines()]

        with pytest.raises(HttpResponseBudgetError) as caught:
            asyncio.run(run())
    assert caught.value.code == "http_response_line_limit"
    assert stream.reads == 2 and stream.closed


@pytest.mark.parametrize(
    "kind",
    [
        "rag",
        "search_api",
        "search_model",
        "llm_json",
        "llm_async_sse",
        "llm_sync_sse",
        "ocr",
        "vision",
    ],
)
@pytest.mark.parametrize("status", [200, 500])
def test_actual_clients_read_bounded_success_and_skip_error_bodies(monkeypatch, kind, status):
    from app.core.config import settings
    from app.llm.deepseek import DeepSeekClient
    from app.search.deepseek import DeepSeekWebSearchClient
    from app.search.deepseek_api import DeepSeekSearchApiClient
    from app.tools.rag_search import RagSearchInput, RagSearchTool
    from app.vision.client import OpenAICompatibleVisionClient
    from app.vision.ocr_client import PaddleOcrHttpClient

    monkeypatch.setattr(settings, "rbac_backend", "mock")
    sync = kind in {"llm_sync_sse", "ocr", "vision"}
    stream = (SyncBytes if sync else AsyncBytes)([b"x" * 65536] * 256)
    transport = httpx.MockTransport(lambda _: httpx.Response(status, stream=stream))
    if sync:
        with (
            httpx.Client(base_url="https://upstream.test", transport=transport) as http,
            pytest.raises(Exception) as caught,
        ):
            if kind == "ocr":
                PaddleOcrHttpClient(
                    base_url="https://upstream.test", provider_version="test", http_client=http
                ).extract(b"synthetic", media_type="image/png")
            elif kind == "vision":
                OpenAICompatibleVisionClient(
                    api_key="synthetic",
                    model="test",
                    base_url="https://upstream.test",
                    http_client=http,
                ).analyze(b"synthetic", media_type="image/png")
            else:
                list(
                    DeepSeekClient(
                        api_key="synthetic", model="test", sync_http_client=http
                    )._stream_events_with_sync_client(http, {})
                )
    else:

        async def run():
            async with httpx.AsyncClient(
                base_url="https://upstream.test", transport=transport
            ) as http:
                if kind == "rag":
                    return await RagSearchTool(client=http).run(RagSearchInput(query="公开制度"))
                if kind == "search_api":
                    return await DeepSeekSearchApiClient(
                        api_key="synthetic", http_client=http
                    ).search(query="公开公告", max_results=1)
                if kind == "search_model":
                    return await DeepSeekWebSearchClient(
                        api_key="synthetic", model="test", http_client=http
                    ).search(query="公开公告", max_results=1)
                model = DeepSeekClient(api_key="synthetic", model="test", http_client=http)
                if kind == "llm_json":
                    return await model.complete(system_prompt="公开回答", user_prompt="公开问题")
                return [event async for event in model._stream_events_with_client(http, {})]

        with pytest.raises(Exception) as caught:
            asyncio.run(run())
    assert stream.closed
    if status == 500:
        assert stream.reads == 0
    else:
        assert stream.reads <= 129
        assert caught.value.code.endswith("response_limit_exceeded")
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_sync_deadline_is_rechecked_after_last_chunk(monkeypatch):
    import app.security.http_response as module

    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    class Late(SyncBytes):
        def __iter__(self):
            yield b"{}"
            clock[0] = 2.0

    stream = Late([])
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as client,
        pytest.raises(HttpResponseBudgetError) as caught,
    ):
        bounded_request(client, "GET", "https://upstream.test", timeout_seconds=1)
    assert caught.value.code == "http_response_deadline" and stream.closed


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_cleanup_failure_does_not_expose_original_secret(mode):
    class BadSync(SyncBytes):
        def close(self):
            self.closed = True
            raise RuntimeError("PRIVATE_CLOSE_CANARY")

    class BadAsync(AsyncBytes):
        async def aclose(self):
            self.closed = True
            raise RuntimeError("PRIVATE_CLOSE_CANARY")

    stream = (BadSync if mode == "sync" else BadAsync)([b"{}"])
    if mode == "sync":
        with (
            httpx.Client(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
            ) as http,
            pytest.raises(HttpResponseBudgetError) as caught,
        ):
            bounded_request(http, "GET", "https://upstream.test", timeout_seconds=1)
    else:

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
            ) as http:
                await async_bounded_request(http, "GET", "https://upstream.test", timeout_seconds=1)

        with pytest.raises(HttpResponseBudgetError) as caught:
            asyncio.run(run())
    assert stream.closed and caught.value.code == "http_response_cleanup"
    assert "PRIVATE_CLOSE_CANARY" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_async_cancel_still_closes_stream():
    async def run():
        started = asyncio.Event()

        class Waiting(AsyncBytes):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b"{}"

        stream = Waiting([])
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as http:
            task = asyncio.create_task(
                async_bounded_request(http, "GET", "https://upstream.test", timeout_seconds=1)
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stream.closed

    asyncio.run(run())


def test_async_deadline_cannot_wait_forever_for_cleanup(monkeypatch):
    import app.security.http_response as module

    monkeypatch.setattr(module, "CLEANUP_TIMEOUT_SECONDS", 0.01, raising=False)

    class StalledClose(AsyncBytes):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b"{}"

        async def aclose(self):
            self.closed = True
            await asyncio.Event().wait()

    stream = StalledClose([])

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as http:
            with pytest.raises(HttpResponseBudgetError):
                await asyncio.wait_for(
                    async_bounded_request(
                        http, "GET", "https://upstream.test", timeout_seconds=0.01
                    ),
                    timeout=0.2,
                )

    asyncio.run(run())
    assert stream.closed


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_sse_utf8_crlf_and_gzip_across_byte_boundaries(mode):
    expected = ["data: " + json.dumps({"text": "中文公开"}, ensure_ascii=False), "", "data: [DONE]"]
    raw = gzip.compress("\r\n".join(expected).encode())
    stream = (SyncBytes if mode == "sync" else AsyncBytes)(
        [raw[i : i + 1] for i in range(len(raw))]
    )
    headers = {"Content-Encoding": "gzip"}
    if mode == "sync":
        with (
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, headers=headers, stream=stream)
                )
            ) as http,
            bounded_stream(http, "GET", "https://upstream.test", timeout_seconds=1) as response,
        ):
            result = list(response.iter_lines())
    else:

        async def run():
            async with (
                httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(200, headers=headers, stream=stream)
                    )
                ) as http,
                async_bounded_stream(
                    http, "GET", "https://upstream.test", timeout_seconds=1
                ) as response,
            ):
                return [line async for line in response.aiter_lines()]

        result = asyncio.run(run())
    assert result == expected and stream.closed


@pytest.mark.parametrize(
    "raw",
    [b'{"password":"PRIVATE_JSON_CANARY"', b'{"contexts":42}', b"[" * 1100 + b"0" + b"]" * 1100],
)
def test_rag_invalid_json_contract_has_fixed_error_without_original_body(monkeypatch, raw):
    from app.core.config import settings
    from app.tools.rag_search import RagSearchError, RagSearchInput, RagSearchTool

    monkeypatch.setattr(settings, "rbac_backend", "mock")

    async def run():
        stream = AsyncBytes([raw])
        async with httpx.AsyncClient(
            base_url="https://upstream.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        ) as http:
            await RagSearchTool(client=http).run(RagSearchInput(query="公开资料"))

    with pytest.raises(RagSearchError) as caught:
        asyncio.run(run())
    assert caught.value.code == "rag_response_invalid"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "PRIVATE_JSON_CANARY" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_deep_sse_json_has_fixed_error_and_does_not_publish_partial_body(mode):
    from app.llm.deepseek import DeepSeekClient
    from app.llm.errors import LlmProviderError

    valid = b'data: {"choices":[{"delta":{"content":"public partial"}}]}\n\n'
    raw = b"data: " + b"[" * 1200 + b"0" + b"]" * 1200 + b"\n\n"
    stream = (SyncBytes if mode == "sync" else AsyncBytes)([valid, raw])
    events = []
    if mode == "sync":
        with (
            httpx.Client(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
                base_url="https://upstream.test",
            ) as http,
            pytest.raises(LlmProviderError) as caught,
        ):
            events.extend(
                DeepSeekClient(api_key="synthetic", model="test")._stream_events_with_sync_client(
                    http, {}
                )
            )
    else:

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
                base_url="https://upstream.test",
            ) as http:
                async for event in DeepSeekClient(
                    api_key="synthetic", model="test"
                )._stream_events_with_client(http, {}):
                    events.append(event)

        with pytest.raises(LlmProviderError) as caught:
            asyncio.run(run())
    assert caught.value.code == "llm_invalid_response" and stream.closed
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not events


def test_response_byte_limit_does_not_enter_model_retry_or_fallback():
    from app.harness.error_boundary import (
        is_retryable_recoverable_error,
        should_route_recoverable_error_to_synthesizer,
    )
    from app.llm.errors import LlmProviderError

    error = LlmProviderError(code="llm_response_limit_exceeded")
    assert not is_retryable_recoverable_error(error)
    assert not should_route_recoverable_error_to_synthesizer(error)


@pytest.mark.parametrize("cleanup", ["error", "timeout"])
def test_async_cancel_wins_over_cleanup_failure(monkeypatch, cleanup, caplog):
    import app.security.http_response as module

    monkeypatch.setattr(module, "CLEANUP_TIMEOUT_SECONDS", 0.01)

    async def run():
        started = asyncio.Event()

        class Waiting(AsyncBytes):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b"{}"

            async def aclose(self):
                self.closed = True
                if cleanup == "error":
                    raise RuntimeError("PRIVATE_CANCEL_CLOSE_CANARY")
                await asyncio.Event().wait()

        stream = Waiting([])
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as http:
            task = asyncio.create_task(
                async_bounded_request(http, "GET", "https://upstream.test", timeout_seconds=1)
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled() and stream.closed

    asyncio.run(run())
    assert "PRIVATE_CANCEL_CLOSE_CANARY" not in caplog.text
