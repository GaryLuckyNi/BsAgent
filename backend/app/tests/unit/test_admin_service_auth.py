"""服务令牌只能用于配置服务，不能沿模型或任意 URL 传播。"""

from importlib import import_module

import httpx
import pytest


def module():
    try:
        return import_module("app.security.admin_service_auth")
    except ModuleNotFoundError:
        pytest.fail("Admin 服务身份适配器尚未实现", pytrace=False)


def auth(handler):
    return module().AdminServiceAuth(
        token_url="https://idp.test/token",
        client_id="runtime",
        client_secret="test-secret",
        scope="admin-runtime",
        allowed_base_urls=["https://admin.test"],
        token_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_service_token_is_cached_and_attached_only_to_runtime_routes():
    calls = []

    def mint(request):
        calls.append(request)
        return httpx.Response(
            200, json={"access_token": "service-token", "token_type": "Bearer", "expires_in": 120}
        )

    adapter = auth(mint)
    received = []
    with httpx.Client(
        auth=adapter,
        transport=httpx.MockTransport(
            lambda request: received.append(request) or httpx.Response(200)
        ),
    ) as client:
        client.get("https://admin.test/api/runtime/skills")
        client.get("https://admin.test/api/internal/market-data-providers/active/mcp-connection")
        with pytest.raises(module().AdminServiceAuthenticationError):
            client.get("https://evil.test/api/runtime/skills")
        with pytest.raises(module().AdminServiceAuthenticationError):
            client.post("https://admin.test/api/skills")
    assert len(calls) == 1
    assert b"grant_type=client_credentials" in calls[0].content
    assert all(r.headers["authorization"] == "Bearer service-token" for r in received)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, text="test-secret"),
        httpx.Response(302, headers={"Location": "https://evil.test"}),
        httpx.Response(200, json={"access_token": "x", "token_type": "Bearer", "expires_in": 0}),
    ],
)
def test_service_token_failure_never_falls_back_to_anonymous(response):
    received = []
    with (
        httpx.Client(
            auth=auth(lambda _: response),
            transport=httpx.MockTransport(
                lambda request: received.append(request) or httpx.Response(200)
            ),
        ) as client,
        pytest.raises(module().AdminServiceAuthenticationError) as exc,
    ):
        client.get("https://admin.test/api/runtime/skills")
    assert "test-secret" not in str(exc.value)
    assert received == []


def test_production_cannot_use_mock_admin_authentication():
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="服务身份"):
        module().build_admin_service_auth(
            SimpleNamespace(admin_auth_mode="mock", app_env="production")
        )


def test_expired_cached_token_is_refreshed(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module(), "monotonic", lambda: clock[0])
    calls = []

    def mint(_):
        calls.append(True)
        return httpx.Response(
            200,
            json={"access_token": f"token-{len(calls)}", "token_type": "Bearer", "expires_in": 20},
        )

    with httpx.Client(
        auth=auth(mint), transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        assert (
            client.get("https://admin.test/api/runtime/skills").request.headers["authorization"]
            == "Bearer token-1"
        )
        clock[0] += 15
        assert (
            client.get("https://admin.test/api/runtime/skills").request.headers["authorization"]
            == "Bearer token-2"
        )
    assert len(calls) == 2


@pytest.mark.parametrize("origin", ["https://admin.test:443", "http://localhost:80"])
def test_service_origin_accepts_explicit_default_port(origin):
    adapter = module().AdminServiceAuth(
        token_url="https://idp.test/token",
        client_id="runtime",
        client_secret="test-secret",
        scope="",
        allowed_base_urls=[origin],
        allow_local_http=True,
        token_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"access_token": "x", "token_type": "Bearer", "expires_in": 120}
                )
            )
        ),
    )
    with httpx.Client(
        auth=adapter, transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        assert client.get(f"{origin}/api/runtime/skills").status_code == 200
