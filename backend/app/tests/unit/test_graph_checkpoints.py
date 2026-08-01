import sys
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from app.graph import checkpoints
from app.graph.checkpoints import (
    CheckpointBackendError,
    build_graph_checkpointer,
    close_graph_checkpointer,
)
from app.graph.runtime import _checkpoint_config


def test_build_graph_checkpointer_returns_sqlite_saver_by_default(monkeypatch, tmp_path):
    build_graph_checkpointer.cache_clear()
    monkeypatch.setattr(checkpoints.settings, "graph_checkpointer_backend", "sqlite")
    monkeypatch.setattr(
        checkpoints.settings,
        "graph_checkpoint_sqlite_path",
        str(tmp_path / "checkpoints.sqlite"),
    )

    checkpointer = build_graph_checkpointer()

    assert isinstance(checkpointer, SqliteSaver)
    assert (tmp_path / "checkpoints.sqlite").exists()


def test_build_graph_checkpointer_can_use_memory_backend():
    build_graph_checkpointer.cache_clear()

    checkpointer = build_graph_checkpointer(backend="memory")

    assert isinstance(checkpointer, InMemorySaver)


def test_build_graph_checkpointer_can_be_disabled():
    build_graph_checkpointer.cache_clear()

    checkpointer = build_graph_checkpointer(backend="none")

    assert checkpointer is None


def test_checkpoint_config_uses_internal_session_key_and_tenant_namespace():
    state = {
        "session_id": "shared-external-session",
        "session_key": "internal-session-key",
        "tenant_id": "tenant-1",
    }

    assert _checkpoint_config(state, enabled=True) == {
        "configurable": {
            "thread_id": "internal-session-key",
            "checkpoint_ns": "tenant-1",
        }
    }
    assert _checkpoint_config(state, enabled=False) is None


def test_build_graph_checkpointer_reuses_same_backend_instance():
    build_graph_checkpointer.cache_clear()

    first = build_graph_checkpointer(backend="memory")
    second = build_graph_checkpointer(backend="memory")

    assert first is second


def test_build_graph_checkpointer_requires_official_sqlite_package(monkeypatch):
    build_graph_checkpointer.cache_clear()
    monkeypatch.delitem(sys.modules, "langgraph.checkpoint.sqlite", raising=False)

    def fake_import_module(name):
        if name == "langgraph.checkpoint.sqlite":
            raise ModuleNotFoundError(name)
        return __import__(name)

    monkeypatch.setattr(checkpoints.importlib, "import_module", fake_import_module)

    with pytest.raises(CheckpointBackendError) as exc_info:
        build_graph_checkpointer(backend="sqlite", sqlite_path="checkpoints.sqlite")

    assert "langgraph-checkpoint-sqlite" in str(exc_info.value)


def test_build_graph_checkpointer_loads_sqlite_saver_lazily(monkeypatch):
    build_graph_checkpointer.cache_clear()

    class FakeSqliteSaver:
        def __init__(self, conn) -> None:
            self.conn = conn
            self.setup_called = False

        def setup(self) -> None:
            self.setup_called = True

    fake_module = ModuleType("langgraph.checkpoint.sqlite")
    fake_module.SqliteSaver = FakeSqliteSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite", fake_module)

    checkpointer = build_graph_checkpointer(backend="sqlite", sqlite_path=":memory:")

    assert isinstance(checkpointer, FakeSqliteSaver)
    assert checkpointer.setup_called is True


def test_build_graph_checkpointer_builds_postgres_saver_with_managed_pool(monkeypatch):
    build_graph_checkpointer.cache_clear()
    captured = {}

    class FakePool:
        def __init__(self, conninfo, **kwargs):
            captured["conninfo"] = conninfo
            captured["pool_kwargs"] = kwargs
            captured["closed"] = False

        def close(self):
            captured["closed"] = True

    class FakePostgresSaver:
        def __init__(self, pool):
            captured["pool"] = pool
            self.setup_called = False

        def setup(self):
            self.setup_called = True

    fake_postgres_module = ModuleType("langgraph.checkpoint.postgres")
    fake_postgres_module.PostgresSaver = FakePostgresSaver
    fake_pool_module = ModuleType("psycopg_pool")
    fake_pool_module.ConnectionPool = FakePool
    monkeypatch.setitem(
        sys.modules,
        "langgraph.checkpoint.postgres",
        fake_postgres_module,
    )
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_pool_module)

    checkpointer = build_graph_checkpointer(
        backend="postgres",
        postgres_url="postgresql://agent@postgres/agent",
    )

    assert isinstance(checkpointer, FakePostgresSaver)
    assert checkpointer.setup_called is True
    assert captured["conninfo"] == "postgresql://agent@postgres/agent"
    assert captured["pool_kwargs"]["open"] is True
    assert captured["pool_kwargs"]["kwargs"] == {
        "autocommit": True,
        "prepare_threshold": 0,
    }

    close_graph_checkpointer()

    assert captured["closed"] is True


def test_graph_checkpointer_cache_clear_closes_previous_postgres_pool(monkeypatch):
    build_graph_checkpointer.cache_clear()
    captured = {"closed": 0}

    class FakePool:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            captured["closed"] += 1

    class FakePostgresSaver:
        def __init__(self, _pool):
            pass

        def setup(self):
            pass

    fake_postgres_module = ModuleType("langgraph.checkpoint.postgres")
    fake_postgres_module.PostgresSaver = FakePostgresSaver
    fake_pool_module = ModuleType("psycopg_pool")
    fake_pool_module.ConnectionPool = FakePool
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", fake_postgres_module)
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_pool_module)

    build_graph_checkpointer(
        backend="postgres",
        postgres_url="postgresql://agent@postgres/agent",
    )
    build_graph_checkpointer.cache_clear()

    assert captured["closed"] == 1


def test_application_lifespan_initializes_and_closes_checkpointer(monkeypatch):
    import app.main as main_module
    from app.operations.health import ReadinessService

    calls: list[str] = []
    monkeypatch.setattr(
        main_module,
        "build_graph_checkpointer",
        lambda: calls.append("build"),
        raising=False,
    )
    monkeypatch.setattr(
        main_module,
        "close_graph_checkpointer",
        lambda: calls.append("close"),
        raising=False,
    )

    app = main_module.create_app(readiness_service=ReadinessService(probes=[]))
    with TestClient(app) as client:
        assert calls == ["build"]
        assert client.get("/health/live").status_code == 200

    assert calls == ["build", "close"]


def test_postgres_setup_failure_closes_pool_immediately(monkeypatch):
    build_graph_checkpointer.cache_clear()
    captured = {"closed": False}

    class FakePool:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            captured["closed"] = True

    class FailingPostgresSaver:
        def __init__(self, _pool):
            pass

        def setup(self):
            raise RuntimeError("migration failed")

    fake_postgres_module = ModuleType("langgraph.checkpoint.postgres")
    fake_postgres_module.PostgresSaver = FailingPostgresSaver
    fake_pool_module = ModuleType("psycopg_pool")
    fake_pool_module.ConnectionPool = FakePool
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", fake_postgres_module)
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_pool_module)

    with pytest.raises(RuntimeError, match="migration failed"):
        build_graph_checkpointer(
            backend="postgres",
            postgres_url="postgresql://agent@postgres/agent",
        )

    assert captured["closed"] is True
