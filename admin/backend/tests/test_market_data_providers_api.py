from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import create_app


TUSHARE_MCP_URL = "https://api.tushare.pro/mcp/token=top-secret"
SECOND_TUSHARE_MCP_URL = "https://api.tushare.pro/mcp/token=second-secret"
MASKED_TUSHARE_MCP_URL = "https://api.tushare.pro/mcp/token=******"


def create_test_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        with testing_session_local() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _provider_payload(
    provider_id: str = "tushare-prod",
    *,
    url: str = TUSHARE_MCP_URL,
    enabled: bool = True,
    active: bool = False,
) -> dict:
    return {
        "id": provider_id,
        "provider": "tushare",
        "display_name": "Tushare Pro",
        "enabled": enabled,
        "active": active,
        "settings": {"mcp_server_url": url},
    }


def test_market_data_provider_rejects_fixture_at_request_validation():
    client = create_test_client()
    response = client.post(
        "/api/market-data-providers",
        json={
            "id": "fixture-local",
            "provider": "fixture",
            "display_name": "本地样例数据",
            "settings": {},
        },
    )

    assert response.status_code == 422


def test_tushare_provider_requires_mcp_url():
    client = create_test_client()
    response = client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "settings": {},
        },
    )

    assert response.status_code == 422
    assert "mcp_server_url" in response.json()["detail"][0]["msg"]


def test_tushare_provider_requires_http_mcp_url():
    client = create_test_client()
    response = client.post(
        "/api/market-data-providers",
        json=_provider_payload(url="file:///tmp/mcp"),
    )

    assert response.status_code == 422
    assert "HTTP(S) URL" in response.json()["detail"][0]["msg"]


def test_tushare_provider_rejects_masked_mcp_url():
    client = create_test_client()
    response = client.post(
        "/api/market-data-providers",
        json=_provider_payload(url=MASKED_TUSHARE_MCP_URL),
    )

    assert response.status_code == 422
    assert "脱敏占位值" in response.json()["detail"][0]["msg"]


def test_market_data_provider_rejects_non_object_settings_in_chinese():
    client = create_test_client()
    response = client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "settings": [TUSHARE_MCP_URL],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "数据源 settings 必须是 JSON object。"


def test_public_provider_responses_mask_mcp_credentials():
    client = create_test_client()
    created = client.post("/api/market-data-providers", json=_provider_payload())

    assert created.status_code == 201
    assert created.json()["settings"]["mcp_server_url"] == MASKED_TUSHARE_MCP_URL

    detail = client.get("/api/market-data-providers/tushare-prod")
    listed = client.get("/api/market-data-providers")

    assert detail.json()["settings"]["mcp_server_url"] == MASKED_TUSHARE_MCP_URL
    assert listed.json()["items"][0]["settings"]["mcp_server_url"] == (
        MASKED_TUSHARE_MCP_URL
    )


def test_market_data_provider_partial_update_preserves_saved_url_and_extra_settings():
    client = create_test_client()
    payload = _provider_payload()
    payload["settings"]["timeout_seconds"] = 10
    assert client.post("/api/market-data-providers", json=payload).status_code == 201

    updated = client.put(
        "/api/market-data-providers/tushare-prod",
        json={"enabled": False, "settings": {"mcp_server_url": ""}},
    )

    assert updated.status_code == 200
    assert updated.json()["enabled"] is False
    assert updated.json()["settings"]["mcp_server_url"] == MASKED_TUSHARE_MCP_URL
    assert updated.json()["settings"]["timeout_seconds"] == 10


def test_market_data_provider_update_replaces_saved_url():
    client = create_test_client()
    assert client.post(
        "/api/market-data-providers", json=_provider_payload()
    ).status_code == 201

    updated = client.put(
        "/api/market-data-providers/tushare-prod",
        json={"settings": {"mcp_server_url": SECOND_TUSHARE_MCP_URL}},
    )

    assert updated.status_code == 200
    assert updated.json()["settings"]["mcp_server_url"] == MASKED_TUSHARE_MCP_URL


def test_market_data_provider_crud_and_active_switch():
    client = create_test_client()
    assert client.post(
        "/api/market-data-providers",
        json=_provider_payload("tushare-local", active=True),
    ).status_code == 201
    assert client.post(
        "/api/market-data-providers",
        json=_provider_payload("tushare-prod", url=SECOND_TUSHARE_MCP_URL),
    ).status_code == 201

    activated = client.post("/api/market-data-providers/tushare-prod/activate")
    active = client.get("/api/market-data-providers/active")
    local = client.get("/api/market-data-providers/tushare-local")

    assert activated.status_code == 200
    assert active.status_code == 200
    assert active.json()["id"] == "tushare-prod"
    assert active.json()["settings"]["mcp_server_url"] == MASKED_TUSHARE_MCP_URL
    assert local.json()["active"] is False


def test_active_provider_reports_legacy_invalid_settings_but_list_remains_editable():
    client = create_test_client()
    assert client.post(
        "/api/market-data-providers",
        json=_provider_payload("legacy-tushare", active=True),
    ).status_code == 201

    with client.app.dependency_overrides[get_db]().__next__() as session:
        session.execute(
            text(
                "UPDATE market_data_provider_configs SET settings = :settings "
                "WHERE id = 'legacy-tushare'"
            ),
            {"settings": '{"token_env_var": "TUSHARE_TOKEN"}'},
        )
        session.commit()

    listed = client.get("/api/market-data-providers")
    detail = client.get("/api/market-data-providers/legacy-tushare")
    active = client.get("/api/market-data-providers/active")

    assert listed.status_code == 200
    assert listed.json()["items"][0]["settings"] == {
        "token_env_var": "TUSHARE_TOKEN"
    }
    assert detail.status_code == 200
    assert active.status_code == 409
    assert active.json()["detail"]["code"] == "market_data_provider_config_invalid"
    assert "mcp_server_url" in active.json()["detail"]["message"]


def test_market_data_provider_rejects_disabled_activation():
    client = create_test_client()
    assert client.post(
        "/api/market-data-providers",
        json=_provider_payload("disabled-tushare", enabled=False),
    ).status_code == 201

    response = client.post("/api/market-data-providers/disabled-tushare/activate")

    assert response.status_code == 400
    assert response.json()["detail"] == "不能激活已停用的数据源配置。"


def test_internal_active_mcp_connection_returns_unmasked_url_and_revision():
    client = create_test_client()
    assert client.post(
        "/api/market-data-providers",
        json=_provider_payload(active=True),
    ).status_code == 201

    response = client.get(
        "/api/internal/market-data-providers/active/mcp-connection"
    )

    assert response.status_code == 200
    assert response.json()["server_url"] == TUSHARE_MCP_URL
    assert response.json()["server_name"] == "tushare"
    assert response.json()["provider_id"] == "tushare-prod"
    assert len(response.json()["config_revision"]) == 64


def test_internal_active_mcp_connection_returns_404_without_active_provider():
    client = create_test_client()

    response = client.get(
        "/api/internal/market-data-providers/active/mcp-connection"
    )

    assert response.status_code == 404


def test_internal_active_mcp_connection_returns_409_for_invalid_active_provider():
    client = create_test_client()
    assert client.post(
        "/api/market-data-providers",
        json=_provider_payload(active=True),
    ).status_code == 201
    with client.app.dependency_overrides[get_db]().__next__() as session:
        session.execute(
            text(
                "UPDATE market_data_provider_configs SET settings = :settings "
                "WHERE id = 'tushare-prod'"
            ),
            {"settings": "{}"},
        )
        session.commit()

    response = client.get(
        "/api/internal/market-data-providers/active/mcp-connection"
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "market_data_provider_config_invalid"
