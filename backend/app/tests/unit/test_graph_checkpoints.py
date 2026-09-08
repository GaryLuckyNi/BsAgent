from hashlib import sha256
import inspect
import sys
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from app.graph import checkpoints, runtime
from app.graph.checkpoints import (
    CheckpointBackendError,
    build_graph_checkpointer,
    close_graph_checkpointer,
)


def test_build_graph_checkpointer_requires_postgres_url_by_default(monkeypatch):
    build_graph_checkpointer.cache_clear()
    monkeypatch.setattr(checkpoints.settings, "graph_checkpoint_postgres_url", None)

    with pytest.raises(CheckpointBackendError, match="PostgreSQL"):
        build_graph_checkpointer()


def test_checkpoint_thread_id_is_stable_tenant_scoped_and_opaque():
    expected_digest = sha256(
        b"tenant-1\0internal-session-key\0request-1"
    ).hexdigest()
    expected = f"cpr1_{expected_digest}"

    first = runtime.build_checkpoint_thread_id(
        tenant_id="tenant-1",
        session_key="internal-session-key",
        request_id="request-1",
    )
    repeated = runtime.build_checkpoint_thread_id(
        tenant_id="tenant-1",
        session_key="internal-session-key",
        request_id="request-1",
    )
    other_tenant = runtime.build_checkpoint_thread_id(
        tenant_id="tenant-2",
        session_key="internal-session-key",
        request_id="request-1",
    )
    other_request = runtime.build_checkpoint_thread_id(
        tenant_id="tenant-1",
        session_key="internal-session-key",
        request_id="request-2",
    )

    assert first == repeated == expected
    assert other_tenant != first
    assert other_request != first
    assert "tenant-1" not in first
    assert "internal-session-key" not in first


def test_checkpoint_config_uses_derived_thread_id_root_namespace_and_recursion_limit(
    monkeypatch,
):
    monkeypatch.setattr(runtime.settings, "graph_recursion_limit", 32, raising=False)
    state = {
        "session_id": "shared-external-session",
        "session_key": "internal-session-key",
        "tenant_id": "tenant-1",
        "request_id": "request-1",
    }

    assert runtime._checkpoint_config(state, enabled=True) == {
        "recursion_limit": 32,
        "configurable": {
            "thread_id": runtime.build_checkpoint_thread_id(
                tenant_id="tenant-1",
                session_key="internal-session-key",
                request_id="request-1",
            ),
            "checkpoint_ns": "",
        }
    }
    assert runtime._checkpoint_config(state, enabled=False) == {"recursion_limit": 32}


@pytest.mark.parametrize(
    "function_name",
    [
        "build_question_answer_graph",
        "run_question_answer_graph",
        "stream_question_answer_graph",
        "resume_question_answer_graph",
    ],
)
def test_graph_runtime_requires_explicit_business_intent_catalog(function_name):
    entrypoint = getattr(runtime, function_name)
    parameter = inspect.signature(entrypoint).parameters["business_intent_catalog"]

    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError, match="business_intent_catalog"):
        entrypoint()


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
        postgres_url="postgresql://agent@postgres/agent",
    )
    build_graph_checkpointer.cache_clear()

    assert captured["closed"] == 1


def test_application_lifespan_initializes_and_closes_checkpointer(
    fake_business_intent_catalog_runtime,
    monkeypatch,
):
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

    app = main_module.create_app(
        readiness_service=ReadinessService(probes=[]),
        business_intent_catalog_runtime=fake_business_intent_catalog_runtime,
    )
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
            postgres_url="postgresql://agent@postgres/agent",
        )

    assert captured["closed"] is True
