import importlib


def test_health_endpoint_returns_ok(
    fake_business_intent_catalog_runtime,
    monkeypatch,
):
    monkeypatch.delenv("BS_AGENT_SERVICE_NAME", raising=False)

    from fastapi.testclient import TestClient

    import app.api.health as health_module
    import app.core.config as config_module
    import app.main as main_module

    importlib.reload(config_module)
    importlib.reload(health_module)
    importlib.reload(main_module)

    client = TestClient(
        main_module.create_app(business_intent_catalog_runtime=fake_business_intent_catalog_runtime)
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "bs-agent-service"}
