"""HTTP 原始/解压后字节与行预算；正文检查前先约束读取资源。"""

import asyncio
import logging
import math
import time
import zlib
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

import httpx

from app.security.run_budget import RunBudgetError
from app.security.run_budget_http import (
    check_single_dispatch_client,
    request_budget,
    response_meter,
)

logger = logging.getLogger(__name__)


class HttpResponseBudgetError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("上游响应超出读取限制或格式不受支持。")


@dataclass(frozen=True)
class HttpResponseLimits:
    max_wire_bytes: int = 8 * 1024 * 1024
    max_decoded_bytes: int = 8 * 1024 * 1024
    max_line_bytes: int = 256 * 1024

    def __post_init__(self):
        if any(
            type(v) is not int or v <= 0
            for v in (
                self.max_wire_bytes,
                self.max_decoded_bytes,
                self.max_line_bytes,
            )
        ):
            raise ValueError("HTTP 响应预算必须为正整数。")


DEFAULT_HTTP_RESPONSE_LIMITS = HttpResponseLimits()
CLEANUP_TIMEOUT_SECONDS = 1.0


def close_http_response(response, *, interrupted=False):
    failed = False
    try:
        response.close()
    except Exception:  # noqa: BLE001 - 关闭第三方响应只返回固定原因。
        failed = True
    if failed:
        if not interrupted:
            raise HttpResponseBudgetError("http_response_cleanup")
        _note_control_cleanup_failure()


async def aclose_http_response(response, *, interrupted=False):
    failed = False
    try:
        await asyncio.wait_for(response.aclose(), timeout=CLEANUP_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - 读取取消后关闭也必须有独立宽限。
        failed = True
    if failed:
        if not interrupted:
            raise HttpResponseBudgetError("http_response_cleanup")
        _note_control_cleanup_failure()


def _note_control_cleanup_failure():
    try:
        logger.warning("http_response_cleanup_failed_during_control_exit")
    except Exception:  # noqa: BLE001, S110 - 保持原控制信号，不能递归写日志。
        pass


class HttpResponseReader:
    def __init__(self, response, limits, deadline):
        self.response, self.limits, self.deadline = response, limits, deadline
        self.meter = response_meter(response)
        self.wire = self.decoded = 0
        self.cached = response.is_stream_consumed
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"identity", "gzip", "deflate"}:
            raise HttpResponseBudgetError("http_response_encoding")
        length = response.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal() or len(length) > 20:
                raise HttpResponseBudgetError("http_response_length")
            if int(length) > limits.max_wire_bytes:
                raise HttpResponseBudgetError("http_response_wire_limit")
        self.decoder = (
            None
            if self.cached or encoding == "identity"
            else zlib.decompressobj(zlib.MAX_WBITS | 16 if encoding == "gzip" else zlib.MAX_WBITS)
        )
        self.deflate_probe = bytearray() if not self.cached and encoding == "deflate" else None

    def check_time(self):
        if self.meter is not None:
            self.meter.check_time()
        if time.monotonic() >= self.deadline:
            raise HttpResponseBudgetError("http_response_deadline")

    def feed(self, raw):
        self.check_time()
        self.wire += len(raw)
        if self.meter is not None:
            self.meter.consume("wire", len(raw))
        if self.wire > self.limits.max_wire_bytes:
            raise HttpResponseBudgetError("http_response_wire_limit")
        if self.decoder is None:
            data = raw
            if self.meter is not None:
                self.meter.consume("decoded", len(data))
        else:
            data = self._inflate(raw)
            if self.decoder.unused_data:
                raise HttpResponseBudgetError("http_response_compression")
        self.decoded += len(data)
        if self.decoded > self.limits.max_decoded_bytes:
            raise HttpResponseBudgetError("http_response_decoded_limit")
        self.check_time()
        return data

    def _inflate(self, raw):
        # 与 httpx 保持 raw-DEFLATE 首次回退兼容；每次解压仍有独立输出上限。
        first_deflate = self.deflate_probe is not None
        if first_deflate:
            self.deflate_probe.extend(raw)
            if len(self.deflate_probe) < 2:
                return b""
            raw = bytes(self.deflate_probe)
            self.deflate_probe = None
        attempts = (
            (self.decoder, zlib.decompressobj(-zlib.MAX_WBITS))
            if first_deflate
            else (self.decoder,)
        )
        for decoder in attempts:
            failed = False
            try:
                if self.meter is None:
                    data = decoder.decompress(raw, self.limits.max_decoded_bytes - self.decoded + 1)
                else:
                    parts, total, pending = [], 0, raw
                    while True:
                        # 无已授予解压信用时只探测一个字节，拒绝前不能先膨胀8MiB。
                        window = min(
                            self.meter.allowance("decoded") or 1,
                            self.limits.max_decoded_bytes - self.decoded - total + 1,
                        )
                        part = decoder.decompress(pending, window)
                        self.meter.consume("decoded", len(part))
                        total += len(part)
                        if self.decoded + total > self.limits.max_decoded_bytes:
                            raise HttpResponseBudgetError("http_response_decoded_limit")
                        parts.append(part)
                        pending = decoder.unconsumed_tail
                        if not pending and (len(part) < window or decoder.eof):
                            break
                    data = b"".join(parts)
            except zlib.error:
                failed = True
            if not failed:
                self.decoder = decoder
                return data
        raise HttpResponseBudgetError("http_response_compression")

    def finish(self):
        self.check_time()
        if self.decoder is not None and not self.decoder.eof:
            raise HttpResponseBudgetError("http_response_compression")

    def iter_bytes(self):
        # 真实 stream=True 响应走原始字节；注入方已预缓冲时只能事后检查其长度。
        if self.cached:
            yield self.feed(self.response.content)
        else:
            failure = None
            try:
                for raw in self.response.iter_raw():
                    yield self.feed(raw)
            except (httpx.HTTPError, HttpResponseBudgetError, RunBudgetError):
                raise
            except Exception:  # noqa: BLE001 - httpx 在 EOF 内自动 close，可能先抛第三方异常。
                failure = HttpResponseBudgetError(
                    "http_response_cleanup" if self.response.is_closed else "http_response_stream"
                )
            if failure is not None:
                raise failure
        self.finish()

    async def aiter_bytes(self):
        if self.cached:
            yield await self._async_feed(self.response.content)
        else:
            failure = None
            try:
                async for raw in self.response.aiter_raw():
                    yield await self._async_feed(raw)
            except (httpx.HTTPError, HttpResponseBudgetError, RunBudgetError):
                raise
            except Exception:  # noqa: BLE001 - 同步/异步 EOF 关闭使用同一固定失败契约。
                failure = HttpResponseBudgetError(
                    "http_response_cleanup" if self.response.is_closed else "http_response_stream"
                )
            if failure is not None:
                raise failure
        self.finish()

    async def _async_feed(self, raw):
        # 仅需预留信用或解压时移出事件循环；已获信用的小片仍走内存快路径。
        if self.meter is not None and (
            self.decoder is not None
            or self.meter.allowance("wire") < len(raw)
            or self.meter.allowance("decoded") < len(raw)
        ):
            return await asyncio.to_thread(self.feed, raw)
        return self.feed(raw)


class _Lines:
    def __init__(self, limit):
        self.limit, self.pending, self.skip_lf = limit, bytearray(), False

    def feed(self, data):
        # SSE 允许 CR、LF、CRLF；UTF-8 只在完整一行后解码。
        for byte in data:
            if self.skip_lf:
                self.skip_lf = False
                if byte == 10:
                    continue
            if byte in (10, 13):
                yield bytes(self.pending).decode("utf-8", errors="replace")
                self.pending.clear()
                self.skip_lf = byte == 13
            else:
                self.pending.append(byte)
                if len(self.pending) > self.limit:
                    raise HttpResponseBudgetError("http_response_line_limit")

    def finish(self):
        if self.pending:
            yield bytes(self.pending).decode("utf-8", errors="replace")


class _Response:
    def __init__(self, response, limits, deadline):
        self.response, self.limits, self.deadline = response, limits, deadline

    def raise_for_status(self):
        # 错误响应不读取正文，也不携带原始 URL/请求凭证。
        _copy(self.response.status_code, b"").raise_for_status()

    def iter_lines(self):
        body, lines = (
            HttpResponseReader(self.response, self.limits, self.deadline),
            _Lines(self.limits.max_line_bytes),
        )
        for data in body.iter_bytes():
            yield from lines.feed(data)
        yield from lines.finish()

    async def aiter_lines(self):
        body, lines = (
            HttpResponseReader(self.response, self.limits, self.deadline),
            _Lines(self.limits.max_line_bytes),
        )
        async for data in body.aiter_bytes():
            for line in lines.feed(data):
                yield line
        for line in lines.finish():
            yield line


def _copy(status, body):
    return httpx.Response(
        status, content=body, request=httpx.Request("GET", "https://upstream.invalid")
    )


def _request(client, method, url, timeout_seconds, kwargs):
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("HTTP 超时必须为有限正数。")
    headers = httpx.Headers(kwargs.pop("headers", None))
    headers["Accept-Encoding"] = "identity"
    return client.build_request(method, url, headers=headers, timeout=timeout_seconds, **kwargs)


@contextmanager
def bounded_stream(
    client,
    method,
    url,
    *,
    timeout_seconds,
    limits=DEFAULT_HTTP_RESPONSE_LIMITS,
    budget_kind=None,
    **kwargs,
):
    request = _request(client, method, url, timeout_seconds, kwargs)
    deadline = time.monotonic() + timeout_seconds
    check_single_dispatch_client(client)
    with request_budget(request, kind=budget_kind):
        response = client.send(request, stream=True, follow_redirects=False)
    interrupted = False
    try:
        if time.monotonic() >= deadline:
            raise HttpResponseBudgetError("http_response_deadline")
        yield _Response(response, limits, deadline)
    except BaseException as error:
        interrupted = not isinstance(error, Exception)
        raise
    finally:
        close_http_response(response, interrupted=interrupted)


@asynccontextmanager
async def async_bounded_stream(
    client,
    method,
    url,
    *,
    timeout_seconds,
    limits=DEFAULT_HTTP_RESPONSE_LIMITS,
    budget_kind=None,
    **kwargs,
):
    request = _request(client, method, url, timeout_seconds, kwargs)
    deadline = time.monotonic() + timeout_seconds
    check_single_dispatch_client(client)
    expired = False
    try:
        async with asyncio.timeout(timeout_seconds):
            with request_budget(request, kind=budget_kind):
                response = await client.send(request, stream=True, follow_redirects=False)
            interrupted = False
            try:
                yield _Response(response, limits, deadline)
            except BaseException as error:
                interrupted = not isinstance(error, Exception)
                raise
            finally:
                await aclose_http_response(response, interrupted=interrupted)
    except TimeoutError:
        expired = True
    if expired:
        raise HttpResponseBudgetError("http_response_deadline")


def bounded_request(
    client, method, url, *, timeout_seconds, limits=DEFAULT_HTTP_RESPONSE_LIMITS, **kwargs
):
    failure = None
    try:
        with bounded_stream(
            client, method, url, timeout_seconds=timeout_seconds, limits=limits, **kwargs
        ) as view:
            response = view.response
            data = (
                b""
                if not 200 <= response.status_code < 300
                else b"".join(HttpResponseReader(response, limits, view.deadline).iter_bytes())
            )
            result = _copy(response.status_code, data)
    except HttpResponseBudgetError as error:
        failure = HttpResponseBudgetError(error.code)
    if failure is not None:
        raise failure
    return result


async def async_bounded_request(
    client, method, url, *, timeout_seconds, limits=DEFAULT_HTTP_RESPONSE_LIMITS, **kwargs
):
    failure = None
    try:
        async with async_bounded_stream(
            client, method, url, timeout_seconds=timeout_seconds, limits=limits, **kwargs
        ) as view:
            response = view.response
            parts = []
            if 200 <= response.status_code < 300:
                async for data in HttpResponseReader(response, limits, view.deadline).aiter_bytes():
                    parts.append(data)
            result = _copy(response.status_code, b"".join(parts))
    except HttpResponseBudgetError as error:
        # async generator athrow 可能把原始取消挂为 context，在公共返回边界重建。
        failure = HttpResponseBudgetError(error.code)
    if failure is not None:
        raise failure
    return result
