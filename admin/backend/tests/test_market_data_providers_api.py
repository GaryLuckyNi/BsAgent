from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import create_app


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


def test_market_data_provider_requires_tushare_enabled_api_names():
    client = create_test_client()
    response = client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "settings": {"token_env_var": "TUSHARE_TOKEN"},
        },
    )

    assert response.status_code == 422


def test_market_data_provider_rejects_non_object_settings_on_create_in_chinese():
    client = create_test_client()
    response = client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "settings": ["TUSHARE_TOKEN", ["daily"]],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "数据源 settings 必须是 JSON object。"


def test_market_data_provider_rejects_non_object_settings_on_update_in_chinese():
    client = create_test_client()
    create_response = client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "settings": {
                "token_env_var": "TUSHARE_TOKEN",
                "enabled_api_names": ["daily"],
            },
        },
    )
    assert create_response.status_code == 201

    response = client.put(
        "/api/market-data-providers/tushare-prod",
        json={"settings": ["daily_basic"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "数据源 settings 必须是 JSON object。"


def test_market_data_provider_normalizes_enabled_api_names_before_persisting():
    client = create_test_client()
    response = client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "settings": {
                "token_env_var": "TUSHARE_TOKEN",
                "enabled_api_names": [" daily ", "daily", "daily_basic"],
            },
        },
    )

    assert response.status_code == 201
    assert response.json()["settings"]["enabled_api_names"] == ["daily", "daily_basic"]

    persisted = client.get("/api/market-data-providers/tushare-prod")
    assert persisted.status_code == 200
    assert persisted.json()["settings"]["enabled_api_names"] == ["daily", "daily_basic"]


def test_market_data_provider_update_partial_patch_uses_saved_settings():
    client = create_test_client()
    create_response = client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "settings": {
                "token_env_var": "TUSHARE_TOKEN",
                "enabled_api_names": [" daily ", "daily"],
            },
        },
    )
    assert create_response.status_code == 201

    response = client.put(
        "/api/market-data-providers/tushare-prod",
        json={"enabled": False},
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["settings"]["enabled_api_names"] == ["daily"]


def test_market_data_provider_rejects_empty_enabled_api_names_on_update():
    client = create_test_client()
    create_response = client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "active": True,
            "settings": {
                "token_env_var": "TUSHARE_TOKEN",
                "enabled_api_names": ["daily"],
            },
        },
    )
    assert create_response.status_code == 201

    response = client.put(
        "/api/market-data-providers/tushare-prod",
        json={"settings": {"enabled_api_names": [" ", ""]}},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Tushare 数据源必须配置非空 settings.enabled_api_names。"
    )

    unchanged = client.get("/api/market-data-providers/tushare-prod")
    assert unchanged.status_code == 200
    assert unchanged.json()["settings"]["token_env_var"] == "TUSHARE_TOKEN"
    assert unchanged.json()["settings"]["enabled_api_names"] == ["daily"]
    assert unchanged.json()["active"] is True
    assert unchanged.json()["enabled"] is True


def test_market_data_provider_preserves_extra_settings_across_read_and_partial_patch():
    client = create_test_client()
    create_response = client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "settings": {
                "token_env_var": "TUSHARE_TOKEN",
                "enabled_api_names": ["daily"],
                "base_url": "https://api.tushare.pro",
                "timeout_seconds": 10,
            },
        },
    )

    assert create_response.status_code == 201
    assert create_response.json()["settings"]["base_url"] == "https://api.tushare.pro"
    assert create_response.json()["settings"]["timeout_seconds"] == 10

    read_response = client.get("/api/market-data-providers/tushare-prod")
    assert read_response.status_code == 200
    assert read_response.json()["settings"]["base_url"] == "https://api.tushare.pro"
    assert read_response.json()["settings"]["timeout_seconds"] == 10

    update_response = client.put(
        "/api/market-data-providers/tushare-prod",
        json={"settings": {"enabled_api_names": [" daily_basic "]}},
    )

    assert update_response.status_code == 200
    assert update_response.json()["settings"]["token_env_var"] == "TUSHARE_TOKEN"
    assert update_response.json()["settings"]["enabled_api_names"] == ["daily_basic"]
    assert update_response.json()["settings"]["base_url"] == "https://api.tushare.pro"
    assert update_response.json()["settings"]["timeout_seconds"] == 10

    patched_read_response = client.get("/api/market-data-providers/tushare-prod")
    assert patched_read_response.status_code == 200
    assert patched_read_response.json()["settings"]["base_url"] == "https://api.tushare.pro"
    assert patched_read_response.json()["settings"]["timeout_seconds"] == 10


def test_market_data_provider_crud_and_active_switch():
    client = create_test_client()
    local_payload = {
        "id": "tushare-local",
        "provider": "tushare",
        "display_name": "本地 Tushare",
        "enabled": True,
        "active": True,
        "settings": {
            "token_env_var": "TUSHARE_TOKEN_LOCAL",
            "enabled_api_names": ["daily"],
        },
    }
    tushare_payload = {
        "id": "tushare-prod",
        "provider": "tushare",
        "display_name": "Tushare Pro",
        "enabled": True,
        "active": False,
        "settings": {
            "token_env_var": "TUSHARE_TOKEN",
            "enabled_api_names": ["daily", "daily_basic"],
            "base_url": "https://api.tushare.pro",
            "timeout_seconds": 10,
        },
    }

    assert client.post("/api/market-data-providers", json=local_payload).status_code == 201
    assert client.post("/api/market-data-providers", json=tushare_payload).status_code == 201

    listed = client.get("/api/market-data-providers")
    assert listed.status_code == 200
    assert listed.json()["total"] == 2

    activated = client.post("/api/market-data-providers/tushare-prod/activate")
    assert activated.status_code == 200
    assert activated.json()["active"] is True

    active = client.get("/api/market-data-providers/active")
    assert active.status_code == 200
    assert active.json()["id"] == "tushare-prod"
    assert active.json()["settings"]["token_env_var"] == "TUSHARE_TOKEN"

    local = client.get("/api/market-data-providers/tushare-local")
    assert local.status_code == 200
    assert local.json()["active"] is False


def test_active_market_data_provider_returns_conflict_for_legacy_invalid_settings():
    client = create_test_client()
    response = client.post(
        "/api/market-data-providers",
        json={
            "id": "legacy-tushare",
            "provider": "tushare",
            "display_name": "旧版 Tushare",
            "enabled": True,
            "active": False,
            "settings": {
                "token_env_var": "TUSHARE_TOKEN",
                "enabled_api_names": ["daily"],
            },
        },
    )
    assert response.status_code == 201

    with client.app.dependency_overrides[get_db]().__next__() as session:
        session.execute(
            text(
                "UPDATE market_data_provider_configs "
                "SET active = 1, settings = :settings "
                "WHERE id = 'legacy-tushare'"
            ),
            {"settings": '{"token_env_var": "TUSHARE_TOKEN"}'},
        )
        session.commit()

    active = client.get("/api/market-data-providers/active")

    assert active.status_code == 409
    assert active.json()["detail"]["code"] == "market_data_provider_config_invalid"
    assert "enabled_api_names" in active.json()["detail"]["message"]


def test_market_data_provider_list_and_detail_allow_admin_to_edit_invalid_settings():
    client = create_test_client()
    response = client.post(
        "/api/market-data-providers",
        json={
            "id": "legacy-tushare",
            "provider": "tushare",
            "display_name": "旧版 Tushare",
            "enabled": True,
            "active": True,
            "settings": {
                "token_env_var": "TUSHARE_TOKEN",
                "enabled_api_names": ["daily"],
            },
        },
    )
    assert response.status_code == 201

    with client.app.dependency_overrides[get_db]().__next__() as session:
        session.execute(
            text(
                "UPDATE market_data_provider_configs "
                "SET settings = :settings "
                "WHERE id = 'legacy-tushare'"
            ),
            {"settings": '{"token_env_var": "TUSHARE_TOKEN"}'},
        )
        session.commit()

    listed = client.get("/api/market-data-providers")
    detail = client.get("/api/market-data-providers/legacy-tushare")
    active = client.get("/api/market-data-providers/active")

    assert listed.status_code == 200
    assert listed.json()["items"][0]["settings"] == {"token_env_var": "TUSHARE_TOKEN"}
    assert detail.status_code == 200
    assert detail.json()["settings"] == {"token_env_var": "TUSHARE_TOKEN"}
    assert active.status_code == 409


def test_market_data_provider_rejects_disabled_activation():
    client = create_test_client()
    payload = {
        "id": "disabled-tushare",
        "provider": "tushare",
        "display_name": "停用的数据源",
        "enabled": False,
        "active": False,
        "settings": {
            "token_env_var": "TUSHARE_TOKEN",
            "enabled_api_names": ["daily"],
        },
    }

    assert client.post("/api/market-data-providers", json=payload).status_code == 201
    response = client.post("/api/market-data-providers/disabled-tushare/activate")

    assert response.status_code == 400
    assert response.json()["detail"] == "不能激活已停用的数据源配置。"


def test_market_data_provider_rejects_tushare_update_without_token_env_var():
    client = create_test_client()
    payload = {
        "id": "tushare-prod",
        "provider": "tushare",
        "display_name": "Tushare Pro",
        "enabled": True,
        "active": False,
        "settings": {
            "token_env_var": "TUSHARE_TOKEN",
            "enabled_api_names": ["daily"],
        },
    }

    assert client.post("/api/market-data-providers", json=payload).status_code == 201
    response = client.put(
        "/api/market-data-providers/tushare-prod",
        json={"settings": {"token_env_var": ""}},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Tushare 数据源必须配置 settings.token_env_var。"
