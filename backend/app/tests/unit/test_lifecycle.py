from threading import Event
from types import SimpleNamespace

import pytest
from app.operations.lifecycle import close_resources


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
    from app import main
    from app.conversations import recovery
    from app.core.config import Settings
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    maintained = Event()
    local_store_calls = []
    monkeypatch.setattr(recovery, "recover_orphaned_durable_runs", lambda _engine: maintained.set())
    monkeypatch.setattr(main, "build_graph_checkpointer", lambda: object())
    monkeypatch.setattr(main, "close_graph_checkpointer", lambda: None)
    monkeypatch.setattr(main, "close_llm_client", lambda: None)

    def forbidden():
        local_store_calls.append(True)
        raise AssertionError("生产 API 不应打开本地 turn dispatcher 的 memory store")

    def noop():
        return None

    app = main.create_app(
        database_engine=create_engine("sqlite://"),
        redis_client=SimpleNamespace(close=noop),
        object_store=SimpleNamespace(close=noop),
        memory_store_runtime=SimpleNamespace(open=noop, get=forbidden, close=noop),
        tool_catalog_runtime=SimpleNamespace(open=noop, close=noop),
        business_intent_catalog_runtime=SimpleNamespace(
            open=noop, close=noop, refresh_seconds=3600
        ),
        readiness_service=object(),
        config=Settings(_env_file=None).model_copy(
            update={"app_env": "production", "otel_enabled": False}
        ),
    )
    with TestClient(app):
        assert maintained.wait(2), "生产 API 需要本进程的工具结果补落库维护"
    assert local_store_calls == []
