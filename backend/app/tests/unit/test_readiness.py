import logging
from threading import Event

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from app.operations.health import (
    DependencyProbe,
    ReadinessService,
    build_readiness_service,
)


@pytest.fixture
def inject_test_checkpointer(monkeypatch):
    from app import main as main_module

    class IntentCatalogRuntime:
        refresh_seconds = 3600

        def open(self):
            return object()

        def refresh(self):
            return object()

        def check_health(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(main_module, "build_graph_checkpointer", InMemorySaver)
    monkeypatch.setattr(main_module, "close_graph_checkpointer", lambda: None)
    monkeypatch.setattr(
        main_module,
        "build_business_intent_catalog_runtime",
        lambda *, config: IntentCatalogRuntime(),
    )


class _HealthyEngine:
    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, _statement):
        return None

    def dispose(self):
        return None


class _HealthyObjectStore:
    def check_health(self):
        return None

    def close(self):
        return None


class _HealthyMemoryStoreRuntime:
    def open(self):
        return None

    def check_health(self):
        return None

    def close(self):
        return None


def test_readiness_service_reports_each_dependency_without_leaking_error_details():
    service = ReadinessService(
        probes=[
            DependencyProbe(name="database", check=lambda: None),
            DependencyProbe(
                name="redis",
                check=lambda: (_ for _ in ()).throw(
                    RuntimeError("redis://user:secret@redis.internal:6379/0")
                ),
            ),
        ]
    )

    report = service.check()

    assert report.ready is False
    assert report.components == {"database": "ok", "redis": "unavailable"}
    assert "secret" not in str(report)
    assert "redis.internal" not in str(report)


def test_liveness_does_not_execute_readiness_probes(inject_test_checkpointer):
    calls: list[str] = []
    service = ReadinessService(
        probes=[DependencyProbe(name="database", check=lambda: calls.append("database"))]
    )

    from app.main import create_app

    app = create_app(readiness_service=service)
    with TestClient(app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert calls == []


def test_create_app_does_not_discover_tools_during_app_creation(
    monkeypatch,
    inject_test_checkpointer,
):
    from app import main as main_module

    def fail_if_called():
        raise AssertionError("工具发现不应发生在 ASGI app 创建阶段。")

    monkeypatch.setattr(
        main_module,
        "get_default_tool_registry",
        fail_if_called,
        raising=False,
    )

    app = main_module.create_app(readiness_service=ReadinessService(probes=[]))

    assert app.title == "bs-agent-service"


def test_readiness_returns_503_when_a_required_dependency_is_unavailable(
    inject_test_checkpointer,
):
    service = ReadinessService(
        probes=[
            DependencyProbe(
                name="database",
                check=lambda: (_ for _ in ()).throw(RuntimeError("connection refused")),
            )
        ]
    )

    from app.main import create_app

    app = create_app(readiness_service=service)
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "service": "bs-agent-service",
        "components": {"database": "unavailable"},
    }


def test_readiness_returns_200_when_required_dependencies_are_available(
    inject_test_checkpointer,
):
    service = ReadinessService(
        probes=[DependencyProbe(name="database", check=lambda: None)]
    )

    from app.main import create_app

    app = create_app(readiness_service=service)
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["components"] == {"database": "ok"}


def test_default_readiness_probes_database_redis_object_store_and_memory_store():
    calls: list[str] = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, statement):
            assert str(statement) == "SELECT 1"
            calls.append("database")

    class Engine:
        def connect(self):
            return Connection()

    class Redis:
        def ping(self):
            calls.append("redis")

    class ObjectStore:
        def check_health(self):
            calls.append("object_store")

    class MemoryStoreRuntime:
        def check_health(self):
            calls.append("memory_store")

    service = build_readiness_service(
        engine=Engine(),
        redis_client=Redis(),
        object_store=ObjectStore(),
        memory_store=MemoryStoreRuntime(),
    )

    report = service.check()

    assert report.ready is True
    assert report.components == {
        "database": "ok",
        "redis": "ok",
        "object_store": "ok",
        "memory_store": "ok",
    }
    assert calls == ["database", "redis", "object_store", "memory_store"]


def test_default_readiness_includes_optional_business_intent_catalog_probe():
    calls: list[str] = []

    class IntentCatalogRuntime:
        def check_health(self):
            calls.append("business_intent_catalog")

    report = build_readiness_service(
        engine=_HealthyEngine(),
        redis_client=None,
        object_store=_HealthyObjectStore(),
        memory_store=_HealthyMemoryStoreRuntime(),
        business_intent_catalog=IntentCatalogRuntime(),
    ).check()

    assert report.ready is True
    assert report.components["business_intent_catalog"] == "ok"
    assert calls == ["business_intent_catalog"]


def test_default_intent_catalog_runtime_reuses_admin_base_url_and_bounded_timeout(
    monkeypatch,
):
    from app.core.config import Settings
    from app.intent import dependencies

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"items": [], "total": 0}

    class Client:
        def __init__(self, *, base_url, timeout):
            captured["base_url"] = base_url
            captured["timeout"] = timeout
            captured["closed"] = False

        def get(self, path, *, params):
            captured["request"] = (path, params)
            return Response()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(dependencies.httpx, "Client", Client)
    config = Settings(
        _env_file=None,
        skill_registry_base_url="https://admin.example.test",
        intent_catalog_refresh_seconds=45,
    )

    runtime = dependencies.build_business_intent_catalog_runtime(config=config)
    snapshot = runtime.open()
    runtime.close()

    assert snapshot is not None
    assert snapshot.catalog.all_profiles() == []
    assert runtime.refresh_seconds == 45
    assert captured["base_url"] == "https://admin.example.test"
    assert captured["timeout"].connect == 5
    assert captured["timeout"].read == 5
    assert captured["request"] == (
        "/api/business-intents",
        {"enabled": "true"},
    )
    assert captured["closed"] is True


def test_initial_intent_catalog_failure_allows_startup_but_readiness_is_503(
    inject_test_checkpointer,
):
    calls: list[str] = []

    class IntentCatalogRuntime:
        refresh_seconds = 3600

        def open(self):
            calls.append("open")
            return None

        def refresh(self):
            calls.append("refresh")

        def check_health(self):
            raise RuntimeError("unavailable")

        def close(self):
            calls.append("close")

    from app.main import create_app

    intent_runtime = IntentCatalogRuntime()
    app = create_app(
        database_engine=_HealthyEngine(),
        object_store=_HealthyObjectStore(),
        memory_store_runtime=_HealthyMemoryStoreRuntime(),
        business_intent_catalog_runtime=intent_runtime,
    )

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["components"]["business_intent_catalog"] == "unavailable"
    assert calls == ["open", "close"]


def test_valid_intent_catalog_snapshot_is_ready_and_runtime_is_stored_and_closed(
    inject_test_checkpointer,
):
    calls: list[str] = []

    class IntentCatalogRuntime:
        refresh_seconds = 3600

        def open(self):
            calls.append("open")
            return object()

        def refresh(self):
            calls.append("refresh")

        def check_health(self):
            calls.append("check_health")

        def close(self):
            calls.append("close")

    from app.main import create_app

    intent_runtime = IntentCatalogRuntime()
    app = create_app(
        database_engine=_HealthyEngine(),
        object_store=_HealthyObjectStore(),
        memory_store_runtime=_HealthyMemoryStoreRuntime(),
        business_intent_catalog_runtime=intent_runtime,
    )

    with TestClient(app) as client:
        assert app.state.business_intent_catalog_runtime is intent_runtime
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["components"]["business_intent_catalog"] == "ok"
    assert calls == ["open", "check_health", "close"]


def test_background_intent_refresh_failure_preserves_readiness_and_is_safely_logged(
    inject_test_checkpointer,
    caplog,
):
    from app import main as main_module

    caplog.set_level(logging.WARNING, logger=main_module.__name__)
    refresh_attempted = Event()
    calls: list[str] = []

    class IntentCatalogRuntime:
        refresh_seconds = 0.01

        def open(self):
            calls.append("open")
            return object()

        def refresh(self):
            calls.append("refresh")
            refresh_attempted.set()
            raise RuntimeError("https://admin.test?token=secret")

        def check_health(self):
            calls.append("check_health")

        def close(self):
            calls.append("close")

    app = main_module.create_app(
        database_engine=_HealthyEngine(),
        object_store=_HealthyObjectStore(),
        memory_store_runtime=_HealthyMemoryStoreRuntime(),
        business_intent_catalog_runtime=IntentCatalogRuntime(),
    )

    with TestClient(app) as client:
        assert refresh_attempted.wait(timeout=1)
        assert client.get("/health/ready").status_code == 200

    assert calls[0] == "open"
    assert "refresh" in calls
    assert calls[-1] == "close"
    assert "business_intent_catalog_refresh_failed type=RuntimeError" in caplog.text
    assert "admin.test" not in caplog.text
    assert "secret" not in caplog.text


def test_build_readiness_service_requires_memory_store():
    class ObjectStore:
        def check_health(self):
            return None

    with pytest.raises(TypeError, match="memory_store"):
        build_readiness_service(
            engine=object(),
            redis_client=None,
            object_store=ObjectStore(),
        )


def test_readiness_is_not_ready_when_memory_store_is_unavailable():
    class Engine:
        def connect(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, _statement):
            return None

    class ObjectStore:
        def check_health(self):
            return None

    class MemoryStoreRuntime:
        def check_health(self):
            raise RuntimeError("Postgres unavailable")

    report = build_readiness_service(
        engine=Engine(),
        redis_client=None,
        object_store=ObjectStore(),
        memory_store=MemoryStoreRuntime(),
    ).check()

    assert report.ready is False
    assert report.components["memory_store"] == "unavailable"


def test_app_lifespan_reuses_and_closes_runtime_resources(inject_test_checkpointer):
    calls: list[str] = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, _statement):
            calls.append("database")

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            calls.append("database.close")

    class Redis:
        def ping(self):
            calls.append("redis")

        def close(self):
            calls.append("redis.close")

    class ObjectStore:
        def check_health(self):
            calls.append("object_store")

        def close(self):
            calls.append("object_store.close")

    class MemoryStoreRuntime:
        def open(self):
            calls.append("memory_store.open")

        def check_health(self):
            return None

        def close(self):
            calls.append("memory_store.close")

    from app.main import create_app

    engine = Engine()
    redis = Redis()
    object_store = ObjectStore()
    app = create_app(
        database_engine=engine,
        redis_client=redis,
        object_store=object_store,
        memory_store_runtime=MemoryStoreRuntime(),
    )

    with TestClient(app) as client:
        assert app.state.redis_client is redis
        assert app.state.asset_object_store is object_store
        assert client.get("/health/ready").status_code == 200

    assert calls == [
        "memory_store.open",
        "database",
        "redis",
        "object_store",
        "memory_store.close",
        "object_store.close",
        "redis.close",
        "database.close",
    ]


def test_app_lifespan_opens_store_before_bounded_memory_reconciliation(
    inject_test_checkpointer,
    monkeypatch,
):
    from app import main as main_module

    calls: list[object] = []
    store = object()

    class Engine:
        def dispose(self):
            calls.append("database.close")

    class MemoryStoreRuntime:
        def open(self):
            calls.append("memory_store.open")
            return store

        def close(self):
            calls.append("memory_store.close")

    class ObjectStore:
        def close(self):
            calls.append("object_store.close")

    def reconcile(*, engine, store, limit):
        calls.append(("memory.reconcile", engine, store, limit))
        return 0

    monkeypatch.setattr(main_module, "reconcile_memory_store", reconcile)
    engine = Engine()
    app = main_module.create_app(
        database_engine=engine,
        object_store=ObjectStore(),
        memory_store_runtime=MemoryStoreRuntime(),
        readiness_service=ReadinessService(probes=[]),
    )

    with TestClient(app):
        pass

    assert calls[:2] == [
        "memory_store.open",
        ("memory.reconcile", engine, store, 100),
    ]


def test_app_lifespan_continues_when_memory_reconciliation_fails(
    inject_test_checkpointer,
    monkeypatch,
    caplog,
):
    from app import main as main_module

    caplog.set_level(logging.WARNING, logger=main_module.__name__)

    class Engine:
        def dispose(self):
            return None

    class MemoryStoreRuntime:
        def open(self):
            return object()

        def close(self):
            return None

    class ObjectStore:
        def close(self):
            return None

    def fail_reconciliation(**_kwargs):
        raise RuntimeError("postgresql://secret@example.invalid/db")

    monkeypatch.setattr(
        main_module,
        "reconcile_memory_store",
        fail_reconciliation,
    )
    app = main_module.create_app(
        database_engine=Engine(),
        object_store=ObjectStore(),
        memory_store_runtime=MemoryStoreRuntime(),
        readiness_service=ReadinessService(probes=[]),
    )

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200

    assert "memory_store_startup_reconciliation_failed" in caplog.text
    assert "secret" not in caplog.text


def test_app_lifespan_continues_when_memory_store_open_fails(
    inject_test_checkpointer,
    caplog,
):
    from app import main as main_module

    caplog.set_level(logging.WARNING, logger=main_module.__name__)

    class Engine:
        def dispose(self):
            return None

    class MemoryStoreRuntime:
        def open(self):
            raise RuntimeError("postgresql://secret@example.invalid/db")

        def close(self):
            return None

    class ObjectStore:
        def close(self):
            return None

    app = main_module.create_app(
        database_engine=Engine(),
        object_store=ObjectStore(),
        memory_store_runtime=MemoryStoreRuntime(),
        readiness_service=ReadinessService(probes=[]),
    )

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200

    assert "memory_store_startup_open_failed" in caplog.text
    assert "secret" not in caplog.text
