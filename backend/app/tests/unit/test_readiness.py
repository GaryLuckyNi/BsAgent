from fastapi.testclient import TestClient

from app.operations.health import (
    DependencyProbe,
    ReadinessService,
    build_readiness_service,
)


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


def test_liveness_does_not_execute_readiness_probes():
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


def test_create_app_does_not_discover_tools_during_app_creation(monkeypatch):
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


def test_readiness_returns_503_when_a_required_dependency_is_unavailable():
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


def test_readiness_returns_200_when_required_dependencies_are_available():
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


def test_default_readiness_probes_database_redis_and_object_store():
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

    class ExperienceStore:
        def check_health(self):
            calls.append("experience_store")

    service = build_readiness_service(
        engine=Engine(),
        redis_client=Redis(),
        object_store=ObjectStore(),
        experience_store=ExperienceStore(),
    )

    report = service.check()

    assert report.ready is True
    assert report.components == {
        "database": "ok",
        "redis": "ok",
        "object_store": "ok",
        "experience_store": "ok",
    }
    assert calls == ["database", "redis", "object_store", "experience_store"]


def test_app_lifespan_reuses_and_closes_runtime_resources():
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

    class ExperienceRuntime:
        def open(self):
            calls.append("experience_store.open")

        def close(self):
            calls.append("experience_store.close")

    from app.main import create_app

    engine = Engine()
    redis = Redis()
    object_store = ObjectStore()
    app = create_app(
        database_engine=engine,
        redis_client=redis,
        object_store=object_store,
        experience_store_runtime=ExperienceRuntime(),
    )

    with TestClient(app) as client:
        assert app.state.redis_client is redis
        assert app.state.asset_object_store is object_store
        assert client.get("/health/ready").status_code == 200

    assert calls == [
        "experience_store.open",
        "database",
        "redis",
        "object_store",
        "experience_store.close",
        "object_store.close",
        "redis.close",
        "database.close",
    ]
