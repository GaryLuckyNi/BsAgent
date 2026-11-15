"""公开 httpx 客户端注入点的 SDK 响应预算；不更改 SDK 协议解析。"""

import asyncio
import math
import time
from contextvars import ContextVar

import anyio
import httpx

from app.security.http_response import (
    DEFAULT_HTTP_RESPONSE_LIMITS,
    HttpResponseBudgetError,
    HttpResponseReader,
    aclose_http_response,
    close_http_response,
)
from app.security.run_budget import RunBudgetError
from app.security.run_budget_http import is_cleanup_request, managed_send

_budget_scope = ContextVar("sdk_response_budget_scope", default=None)


class _BudgetScope:
    def __init__(self):
        self.signal, self.error = asyncio.Event(), None

    def record(self, error):
        if isinstance(error, (HttpResponseBudgetError, RunBudgetError)) and self.error is None:
            self.error = (
                type(error)()
                if isinstance(error, RunBudgetError)
                else HttpResponseBudgetError(error.code)
            )
            self.signal.set()


async def _observed_close(response, *, scope=None, interrupted=False):
    try:
        with anyio.CancelScope(shield=True):
            await aclose_http_response(response, interrupted=interrupted)
    except HttpResponseBudgetError as error:
        observer = scope or _budget_scope.get()
        if observer:
            observer.record(error)
        raise


class _RawSyncClose(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        yield from self.stream

    def close(self):
        close_http_response(self.stream)


class _RawAsyncClose(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream, self.scope = stream, _budget_scope.get()

    async def __aiter__(self):
        async for data in self.stream:
            yield data

    async def aclose(self):
        # HTTPX 自己在 EOF 调用此 close，必须在该层限制关闭等待。
        # MCP task group 在收到完整RPC后会取消读任务；给已有关闭尝试保留有界宽限。
        with anyio.CancelScope(shield=True):
            await _observed_close(self.stream, scope=self.scope)


async def run_with_response_budget(operation, *, timeout_seconds):
    """SDK可能吞掉网络错误；仅host标记能中止同一次会话，不能信任工具错误正文。"""
    scope = _BudgetScope()
    token = _budget_scope.set(scope)
    work = asyncio.create_task(operation)
    signal = asyncio.create_task(scope.signal.wait())
    failure = None
    try:
        done, _ = await asyncio.wait(
            (work, signal), timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED
        )
        if scope.error is not None:
            failure = (
                type(scope.error)()
                if isinstance(scope.error, RunBudgetError)
                else HttpResponseBudgetError(scope.error.code)
            )
        elif not done:
            failure = TimeoutError()
        else:
            return await work
    finally:
        for task in (work, signal):
            if not task.done():
                task.cancel()
        try:
            await asyncio.gather(work, signal, return_exceptions=True)
        finally:
            _budget_scope.reset(token)
    if failure is not None:
        raise failure


class _LineBudget:
    def __init__(self, limit):
        self.limit, self.size = limit, 0

    def check(self, data):
        for byte in data:
            self.size = 0 if byte in (10, 13) else self.size + 1
            if self.size > self.limit:
                raise HttpResponseBudgetError("http_response_line_limit")


class _SyncBody(httpx.SyncByteStream):
    def __init__(self, source, reader, lines):
        self.source, self.reader, self.lines = source, reader, lines

    def __iter__(self):
        interrupted = False
        try:
            for data in self.reader.iter_bytes():
                if self.lines:
                    self.lines.check(data)
                yield data
        except BaseException as error:
            interrupted = not isinstance(error, Exception)
            raise
        finally:
            close_http_response(self.source, interrupted=interrupted)

    def close(self):
        close_http_response(self.source)


class _AsyncBody(httpx.AsyncByteStream):
    def __init__(self, source, reader, lines):
        self.source, self.reader, self.lines = source, reader, lines
        self.scope = _budget_scope.get()

    async def __aiter__(self):
        interrupted = False
        iterator = self.reader.aiter_bytes().__aiter__()
        try:
            while True:
                self.reader.check_time()
                expired = False
                try:
                    # 每次等待使用同一绝对截止时间；不把计时上下文跨 yield 留在调用任务。
                    data = await asyncio.wait_for(
                        anext(iterator), timeout=self.reader.deadline - time.monotonic()
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    expired = True
                if expired:
                    raise HttpResponseBudgetError("http_response_deadline")
                if self.lines:
                    self.lines.check(data)
                yield data
        except BaseException as error:
            if self.scope:
                self.scope.record(error)
            interrupted = not isinstance(error, Exception)
            raise
        finally:
            await _observed_close(self.source, scope=self.scope, interrupted=interrupted)

    async def aclose(self):
        await _observed_close(self.source, scope=self.scope)


def _response(source, reader, *, asynchronous, expect_lines):
    headers = httpx.Headers(source.headers)
    headers.pop("content-encoding", None)
    headers.pop("content-length", None)
    headers.pop("transfer-encoding", None)
    is_sse = (
        source.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        == "text/event-stream"
    )
    lines = _LineBudget(reader.limits.max_line_bytes) if expect_lines or is_sse else None
    body = (_AsyncBody if asynchronous else _SyncBody)(source, reader, lines)
    return httpx.Response(
        source.status_code,
        headers=headers,
        stream=body,
        request=source.request,
        extensions=dict(source.extensions),
    )


def _validate_timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("SDK HTTP 超时必须为有限正数。")


class BoundedClient(httpx.Client):
    def __init__(
        self,
        *,
        timeout_seconds,
        delegate=None,
        limits=DEFAULT_HTTP_RESPONSE_LIMITS,
        streaming_is_sse=False,
        budget_kind=None,
        cleanup_reservation=None,
        **kwargs,
    ):
        _validate_timeout(timeout_seconds)
        self._budget_timeout, self._delegate = timeout_seconds, delegate
        self._budget_limits, self._streaming_is_sse = limits, streaming_is_sse
        self._budget_kind, self._cleanup_reservation = budget_kind, cleanup_reservation
        kwargs.setdefault("timeout", timeout_seconds)
        super().__init__(**kwargs)

    def build_request(self, *args, **kwargs):
        builder = (
            self._delegate.build_request if self._delegate is not None else super().build_request
        )
        return builder(*args, **kwargs)

    @managed_send
    def send(self, request, *, stream=False, **kwargs):
        cleanup = is_cleanup_request(request)
        if self.is_closed:
            raise RuntimeError("SDK HTTP 客户端已关闭。")
        deadline = time.monotonic() + self._budget_timeout
        request.headers["Accept-Encoding"] = "identity"
        kwargs["follow_redirects"] = False
        sender = self._delegate.send if self._delegate is not None else super().send
        source = sender(request, stream=True, **kwargs)
        if not source.is_stream_consumed:
            source.stream = _RawSyncClose(source.stream)
        failure = None
        try:
            if cleanup or not 200 <= source.status_code < 300:
                close_http_response(source)
                return httpx.Response(
                    source.status_code, headers=source.headers, content=b"", request=source.request
                )
            reader = HttpResponseReader(source, self._budget_limits, deadline)
            reader.check_time()
            response = _response(
                source, reader, asynchronous=False, expect_lines=stream and self._streaming_is_sse
            )
            if not stream:
                response.read()
        except BaseException as error:
            close_http_response(source, interrupted=not isinstance(error, Exception))
            if not isinstance(error, HttpResponseBudgetError):
                raise
            failure = HttpResponseBudgetError(error.code)
        if failure:
            raise failure
        return response


class BoundedAsyncClient(httpx.AsyncClient):
    def __init__(
        self,
        *,
        timeout_seconds,
        delegate=None,
        limits=DEFAULT_HTTP_RESPONSE_LIMITS,
        streaming_is_sse=False,
        budget_kind=None,
        cleanup_reservation=None,
        **kwargs,
    ):
        _validate_timeout(timeout_seconds)
        self._budget_timeout, self._delegate = timeout_seconds, delegate
        self._budget_limits, self._streaming_is_sse = limits, streaming_is_sse
        self._budget_kind, self._cleanup_reservation = budget_kind, cleanup_reservation
        self._retired = False
        kwargs.setdefault("timeout", timeout_seconds)
        super().__init__(**kwargs)

    @property
    def is_closed(self):
        return self._retired or super().is_closed

    def retire(self):
        # 借用客户端的底层池仍属调用方；只关闭本门面的新请求准入。
        self._retired = True

    def build_request(self, *args, **kwargs):
        builder = (
            self._delegate.build_request if self._delegate is not None else super().build_request
        )
        return builder(*args, **kwargs)

    @managed_send
    async def send(self, request, *, stream=False, **kwargs):
        cleanup = is_cleanup_request(request)
        if self.is_closed:
            raise RuntimeError("SDK HTTP 客户端已关闭。")
        deadline = time.monotonic() + self._budget_timeout
        request.headers["Accept-Encoding"] = "identity"
        kwargs["follow_redirects"] = False
        sender = self._delegate.send if self._delegate is not None else super().send
        expired = False
        try:
            async with asyncio.timeout(
                min(1, self._budget_timeout) if cleanup else self._budget_timeout
            ):
                source = await sender(request, stream=True, **kwargs)
        except TimeoutError:
            expired = True
        if expired:
            error = HttpResponseBudgetError("http_response_deadline")
            scope = _budget_scope.get()
            if scope:
                scope.record(error)
            raise error
        if not source.is_stream_consumed:
            source.stream = _RawAsyncClose(source.stream)
        failure = None
        try:
            if cleanup or not 200 <= source.status_code < 300:
                await _observed_close(source)
                return httpx.Response(
                    source.status_code, headers=source.headers, content=b"", request=source.request
                )
            reader = HttpResponseReader(source, self._budget_limits, deadline)
            reader.check_time()
            response = _response(
                source, reader, asynchronous=True, expect_lines=stream and self._streaming_is_sse
            )
            if not stream:
                await response.aread()
        except BaseException as error:
            scope = _budget_scope.get()
            if scope:
                scope.record(error)
            await _observed_close(source, interrupted=not isinstance(error, Exception))
            if not isinstance(error, HttpResponseBudgetError):
                raise
            failure = HttpResponseBudgetError(error.code)
        if failure:
            raise failure
        return response


class _FacadeTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        raise RuntimeError("SDK 门面不能直接建立连接。")


class _OwnedAsyncBody(httpx.AsyncByteStream):
    def __init__(self, response, client):
        self.response, self.client = response, client

    async def __aiter__(self):
        interrupted = False
        try:
            async for data in self.response.aiter_raw():
                yield data
        except BaseException as error:
            interrupted = not isinstance(error, Exception)
            raise
        finally:
            await aclose_http_response(self.client, interrupted=interrupted)

    async def aclose(self):
        interrupted = False
        try:
            await aclose_http_response(self.response)
        except BaseException as error:
            interrupted = not isinstance(error, Exception)
            raise
        finally:
            await aclose_http_response(self.client, interrupted=interrupted)


class RequestScopedAsyncClient(BoundedAsyncClient):
    """默认原生模型跨多 asyncio.run 使用：每响应拥有连接，门面不持有闲置池。"""

    def __init__(self, *, timeout_seconds, client_factory=None, budget_kind=None):
        super().__init__(timeout_seconds=timeout_seconds, transport=_FacadeTransport())
        self._client_factory = client_factory or (
            lambda: BoundedAsyncClient(
                timeout_seconds=timeout_seconds,
                streaming_is_sse=True,
                budget_kind=budget_kind,
            )
        )

    async def send(self, request, *, stream=False, **kwargs):
        if self.is_closed:
            raise RuntimeError("SDK HTTP 客户端已关闭。")
        client = self._client_factory()
        try:
            response = await client.send(request, stream=stream, **kwargs)
        except BaseException as error:
            await aclose_http_response(client, interrupted=not isinstance(error, Exception))
            raise
        if response.is_closed:
            await aclose_http_response(client)
            return response
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_OwnedAsyncBody(response, client),
            request=response.request,
            extensions=dict(response.extensions),
        )


def find_response_budget_error(error):
    """仅检查有限个异常对象；不序列化 SDK 的请求、响应或异常文本。"""
    pending, seen = [error], set()
    while pending and len(seen) < 32:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, HttpResponseBudgetError):
            return item
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions[:32])
        pending.extend(
            value
            for value in (item.__cause__, item.__context__)
            if isinstance(value, BaseException)
        )
    return None
