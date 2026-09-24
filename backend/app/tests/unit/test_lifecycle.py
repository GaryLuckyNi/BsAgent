from types import SimpleNamespace

import pytest
from app.core.config import Settings
from app.operations.health import ReadinessService
from app.operations.lifecycle import close_resources
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver


def test_close_resources_continues_after_a_closer_fails():
    calls: list[str] = []

    def failing_close() -> None:
        calls.append("failing")
        raise RuntimeError("close failed")

    def later_close() -> None:
        calls.append("later")

    with pytest.raises(RuntimeError, match="close failed"):
        close_resources(failing_close, later_close)

    assert calls == ["failing", "later"]


def test_application_configures_and_closes_langfuse_once(
    fake_business_intent_catalog_runtime,
    monkeypatch,
):
    from app import main as main_module

    calls = []
    langfuse_runtime = SimpleNamespace(
        shutdown=lambda: calls.append("shutdown")
    )

    def configure(*, config):
        calls.append(("configure", config.langfuse_base_url))
        return langfuse_runtime

    monkeypatch.setattr(main_module, "build_graph_checkpointer", InMemorySaver)
    monkeypatch.setattr(main_module, "close_graph_checkpointer", lambda: None)
    app = main_module.create_app(
        readiness_service=ReadinessService(probes=[]),
        database_engine=SimpleNamespace(dispose=lambda: None),
        object_store=SimpleNamespace(close=lambda: None),
        memory_store_runtime=SimpleNamespace(
            open=lambda: None,
            close=lambda: None,
        ),
        business_intent_catalog_runtime=fake_business_intent_catalog_runtime,
        config=Settings(
            _env_file=None,
            app_env="test",
            langfuse_enabled=True,
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        ),
        langfuse_configurer=configure,
    )

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200

    assert calls == [
        ("configure", "https://jp.cloud.langfuse.com"),
        "shutdown",
    ]
    assert app.state.readiness_service.check().components == {}
