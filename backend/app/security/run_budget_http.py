"""可信HTTP派发票据、清理预留及响应信用；远端字段不能授予预算。"""

import asyncio
import inspect
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from threading import Lock

import httpx

from app.security.run_budget import (
    BudgetExpired,
    BudgetUnavailable,
    charge,
    current_budget,
    reserve_response_credit,
    utc_now,
)


@dataclass
class _RequestTicket:
    request: object
    binding: object
    model: bool
    cleanup: bool = False


_ticket = ContextVar("run_budget_http_ticket", default=None)


class _CleanupReservation:
    def __init__(self, binding, url):
        self.binding, self.url, self.used = binding, httpx.URL(url), False
        self.lock = Lock()

    def consume(self, request, binding):
        with self.lock:
            if (
                self.used
                or binding != self.binding
                or request.method != "DELETE"
                or request.url != self.url
                or request.content
            ):
                return False
            self.used = True
            return True


def reserve_cleanup(url):
    binding = current_budget()
    if binding is None:
        return None
    charge(binding, http=1)
    return _CleanupReservation(binding, url)


def check_single_dispatch_client(client, explicit_auth=httpx.USE_CLIENT_DEFAULT):
    if current_budget() is None:
        return
    auth = (
        getattr(client, "auth", None)
        if explicit_auth is httpx.USE_CLIENT_DEFAULT
        else explicit_auth
    )
    basic_tuple = type(auth) is tuple and len(auth) == 2 and all(type(item) is str for item in auth)
    if auth is not None and type(auth) is not httpx.BasicAuth and not basic_tuple:
        raise BudgetUnavailable()
    # 自动auth流和事件hook可能在send内部重发/预读；本阶段只支持单轮认证及无hook传输。
    if any(getattr(client, "event_hooks", {}).values()):
        raise BudgetUnavailable()


@contextmanager
def request_budget(request, *, kind=None, cleanup=None):
    binding = current_budget()
    if binding is None:
        yield False
        return
    old = _ticket.get()
    if old is not None and old.request is request and old.binding == binding:
        if kind == "model" and not old.model:
            charge(binding, models=1)
            old.model = True
        yield old.cleanup
        return
    try:
        size = len(request.content)
    except httpx.RequestNotRead:
        raise BudgetUnavailable() from None
    is_cleanup = isinstance(cleanup, _CleanupReservation) and cleanup.consume(request, binding)
    if not is_cleanup:
        charge(binding, http=1, request_bytes=size, models=int(kind == "model"))
    token = _ticket.set(_RequestTicket(request, binding, kind == "model", is_cleanup))
    try:
        yield is_cleanup
    finally:
        _ticket.reset(token)


@asynccontextmanager
async def async_request_budget(request, *, kind=None, cleanup=None):
    binding = current_budget()
    if binding is None:
        yield False
        return
    old = _ticket.get()
    if old is not None and old.request is request and old.binding == binding:
        if kind == "model" and not old.model:
            await asyncio.to_thread(charge, binding, models=1)
            old.model = True
        yield old.cleanup
        return
    try:
        size = len(request.content)
    except httpx.RequestNotRead:
        raise BudgetUnavailable() from None
    is_cleanup = isinstance(cleanup, _CleanupReservation) and cleanup.consume(request, binding)
    if not is_cleanup:
        await asyncio.to_thread(charge, binding, http=1, request_bytes=size, models=int(kind == "model"))
    # 票据在调用任务设置；不能把线程中的ContextVar变更误当成任务绑定。
    token = _ticket.set(_RequestTicket(request, binding, kind == "model", is_cleanup))
    try:
        yield is_cleanup
    finally:
        _ticket.reset(token)


def managed_send(function):
    """在SDK吞异常之前记录host信号；清理仅消费派发前预留的一次额度。"""
    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def async_send(self, request, *args, **kwargs):
            from app.security.run_budget import RunBudgetError
            from app.security.sdk_http import _budget_scope

            try:
                check_single_dispatch_client(
                    self._delegate or self, kwargs.get("auth", httpx.USE_CLIENT_DEFAULT)
                )
                async with async_request_budget(
                    request, kind=self._budget_kind, cleanup=self._cleanup_reservation
                ):
                    return await function(self, request, *args, **kwargs)
            except RunBudgetError as error:
                scope = _budget_scope.get()
                if scope:
                    scope.record(error)
                raise

        return async_send

    @wraps(function)
    def sync_send(self, request, *args, **kwargs):
        check_single_dispatch_client(
            self._delegate or self, kwargs.get("auth", httpx.USE_CLIENT_DEFAULT)
        )
        with request_budget(request, kind=self._budget_kind, cleanup=self._cleanup_reservation):
            return function(self, request, *args, **kwargs)

    return sync_send


def is_cleanup_request(request):
    ticket = _ticket.get()
    return ticket is not None and ticket.request is request and ticket.cleanup


class ResponseMeter:
    def __init__(self, binding):
        self.binding = binding
        self.credit = {"wire": 0, "decoded": 0}

    def check_time(self):
        if utc_now() >= self.binding.expires_at:
            raise BudgetExpired()

    def allowance(self, dimension):
        self.check_time()
        return self.credit[dimension]

    def consume(self, dimension, size):
        self.check_time()
        while size:
            if not self.credit[dimension]:
                self.credit[dimension] = reserve_response_credit(self.binding, dimension)
            used = min(size, self.credit[dimension])
            self.credit[dimension] -= used
            size -= used


class _MeterStamp:
    def __init__(self, binding):
        self.binding = binding


def response_meter(response):
    binding = current_budget()
    if binding is None:
        return None
    stamp = response.extensions.get("bsagent_run_budget")
    if isinstance(stamp, _MeterStamp) and stamp.binding == binding:
        return None
    response.extensions["bsagent_run_budget"] = _MeterStamp(binding)
    return ResponseMeter(binding)
