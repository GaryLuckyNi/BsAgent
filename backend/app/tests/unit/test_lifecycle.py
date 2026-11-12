from threading import Event
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


def test_production_api_retries_local_receipts_without_dispatching_local_turns(monkeypatch):
    import asyncio

    from app import main
    from app.conversations import recovery
    from app.core.config import Settings
    from fastapi.testclient import TestClient
    from redis import ConnectionPool
    from sqlalchemy import create_engine

    maintained = Event()
    local_store_calls = []
    model_closed = []
    monkeypatch.setattr(recovery, "recover_orphaned_durable_runs", lambda _engine: maintained.set())
    monkeypatch.setattr(main, "build_graph_checkpointer", lambda: object())
    monkeypatch.setattr(main, "close_graph_checkpointer", lambda: None)
    async def close_model():
        await asyncio.sleep(0)
        model_closed.append(True)
    monkeypatch.setattr(main, "aclose_llm_client", close_model)

    def forbidden():
        local_store_calls.append(True)
        raise AssertionError("生产 API 不应打开本地 turn dispatcher 的 memory store")

    def noop():
        return None

    app = main.create_app(
        database_engine=create_engine("sqlite://"),
        redis_client=SimpleNamespace(close=noop, connection_pool=ConnectionPool()),
        object_store=SimpleNamespace(close=noop),
        memory_store_runtime=SimpleNamespace(open=noop, get=forbidden, close=noop),
        tool_catalog_runtime=SimpleNamespace(open=noop, close=noop),
        business_intent_catalog_runtime=SimpleNamespace(
            open=noop, close=noop, refresh_seconds=3600
        ),
        readiness_service=object(),
        config=Settings(_env_file=None).model_copy(
            update={"app_env": "production", "otel_enabled": False, "rate_limit_backend":"redis"}
        ),
    )
    with TestClient(app):
        assert maintained.wait(2), "生产 API 需要本进程的工具结果补落库维护"
    assert local_store_calls == []
    assert model_closed == [True]
def test_application_configures_and_closes_langfuse_once(
    fake_business_intent_catalog_runtime,
    monkeypatch,
):
    from app import main as main_module

    calls = []
    langfuse_runtime = SimpleNamespace(
        client=SimpleNamespace(flush=lambda: calls.append("flush")),
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
            langfuse_base_url="http://localhost:3000",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        ),
        langfuse_configurer=configure,
    )

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200

    assert calls == [
        ("configure", "http://localhost:3000"),
        "shutdown",
    ]
    assert app.state.langfuse_finalizer is not None
    assert app.state.readiness_service.check().components == {}
