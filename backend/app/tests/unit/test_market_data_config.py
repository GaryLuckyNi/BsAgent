import httpx
import pytest

from app.tools.market_data_config import AdminMarketDataConfigClient


def test_admin_market_data_client_returns_active_mcp_config():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "provider_id": "tushare-prod",
                "provider": "tushare",
                "server_name": "tushare",
                "server_url": "https://api.tushare.pro/mcp/token=secret",
                "config_revision": "revision-1",
            },
        )
    )
    client = AdminMarketDataConfigClient(
        base_url="http://admin:8002",
        client=httpx.Client(transport=transport),
    )

    config = client.get_active_mcp_config()

    assert config is not None
    assert config.name == "tushare"
    assert config.url.endswith("token=secret")
    assert config.config_revision == "revision-1"
    assert config.enforce_local_rbac is False
    assert config.permission_denial_is_pre_execution is True


def test_admin_market_data_client_maps_404_to_no_connection():
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    client = AdminMarketDataConfigClient(
        base_url="http://admin:8002",
        client=httpx.Client(transport=transport),
    )

    assert client.get_active_mcp_config() is None


@pytest.mark.parametrize("status_code", [401, 409, 500])
def test_admin_market_data_client_uses_stable_http_error(status_code):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status_code,
            json={"detail": "https://api.tushare.pro/mcp/token=secret"},
        )
    )
    client = AdminMarketDataConfigClient(
        base_url="http://admin:8002",
        client=httpx.Client(transport=transport),
    )

    with pytest.raises(RuntimeError) as exc_info:
        client.get_active_mcp_config()

    assert str(exc_info.value) == f"market_data_config_http_{status_code}"
    assert "secret" not in str(exc_info.value)


def test_admin_market_data_client_rejects_invalid_response_without_leaking_url():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "server_name": "tushare",
                "server_url": "https://api.tushare.pro/mcp/token=secret",
            },
        )
    )
    client = AdminMarketDataConfigClient(
        base_url="http://admin:8002",
        client=httpx.Client(transport=transport),
    )

    with pytest.raises(RuntimeError) as exc_info:
        client.get_active_mcp_config()

    assert str(exc_info.value) == "market_data_config_invalid_response"
    assert "secret" not in str(exc_info.value)


def test_admin_market_data_client_maps_transport_failure_to_stable_error():
    def fail(_request):
        raise httpx.ConnectError("token=secret")

    client = AdminMarketDataConfigClient(
        base_url="http://admin:8002",
        client=httpx.Client(transport=httpx.MockTransport(fail)),
    )

    with pytest.raises(RuntimeError) as exc_info:
        client.get_active_mcp_config()

    assert str(exc_info.value) == "market_data_config_unavailable"
    assert "secret" not in str(exc_info.value)
