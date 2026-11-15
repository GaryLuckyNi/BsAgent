"""显式加载的隔离测试插件；MockTransport与ASGI传输保持可用。"""

import httpx
import pytest


@pytest.fixture(autouse=True)
def block_default_httpx_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("安全回归禁止默认HTTPX网络传输。")

    async def async_blocked(*args, **kwargs):
        raise AssertionError("安全回归禁止默认HTTPX网络传输。")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)
