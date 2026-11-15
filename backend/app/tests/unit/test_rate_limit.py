import pytest
from app.core.config import Settings
from app.operations.rate_limit import (
    RateLimitExceeded,
    RateLimitPolicy,
    RateLimitService,
    RateLimitUnavailable,
    build_rate_limit_service,
    require_rate_limit,
)
from app.security.principal import Principal
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


class FakeLimiter:
    def __init__(self, *, allowed: bool = True, fail: bool = False) -> None:
        self.allowed = allowed
        self.fail = fail
        self.identifiers: tuple[str, ...] = ()

    def hit(self, item, *identifiers, cost=1):
        del item, cost
        if self.fail:
            raise ConnectionError("redis://user:secret@redis.internal:6379")
        self.identifiers = identifiers
        return self.allowed

    def get_window_stats(self, item, *identifiers):
        del item, identifiers
        return (1_900_000_030.0, 0)


def _principal() -> Principal:
    return Principal(
        tenant_id="tenant-sensitive",
        user_id="user-sensitive",
        scopes=frozenset({"agent:invoke"}),
        roles=frozenset(),
    )


def test_rate_limit_uses_hashed_tenant_subject_identity():
    limiter = FakeLimiter()
    service = RateLimitService(limiter=limiter, now=lambda: 1_900_000_000.0)

    service.consume(
        principal=_principal(),
        policy=RateLimitPolicy(name="agent_invocation", requests_per_minute=20),
    )

    joined = ":".join(limiter.identifiers)
    assert limiter.identifiers[0] == "agent_invocation"
    assert "tenant-sensitive" not in joined
    assert "user-sensitive" not in joined


def test_rate_limit_exceeded_exposes_retry_after_without_identity():
    service = RateLimitService(
        limiter=FakeLimiter(allowed=False),
        now=lambda: 1_900_000_000.0,
    )

    with pytest.raises(RateLimitExceeded) as exc_info:
        service.consume(
            principal=_principal(),
            policy=RateLimitPolicy(name="image_upload", requests_per_minute=10),
        )

    assert exc_info.value.retry_after_seconds == 30
    assert "tenant-sensitive" not in str(exc_info.value)


def test_rate_limit_storage_failure_is_fail_closed_when_configured():
    service = RateLimitService(
        limiter=FakeLimiter(fail=True),
        fail_closed=True,
    )

    with pytest.raises(RateLimitUnavailable) as exc_info:
        service.consume(
            principal=_principal(),
            policy=RateLimitPolicy(name="agent_invocation", requests_per_minute=20),
        )

    assert "secret" not in str(exc_info.value)
    assert "redis.internal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None and exc_info.value.__context__ is None


def test_rate_limit_storage_failure_can_fail_open_outside_production():
    service = RateLimitService(
        limiter=FakeLimiter(fail=True),
        fail_closed=False,
    )

    service.consume(
        principal=_principal(),
        policy=RateLimitPolicy(name="agent_invocation", requests_per_minute=20),
    )


def test_rate_limit_dependency_returns_429_with_retry_after():
    policy = RateLimitPolicy(name="agent_invocation", requests_per_minute=20)
    app = FastAPI()
    app.state.rate_limit_service = RateLimitService(
        limiter=FakeLimiter(allowed=False),
        now=lambda: 1_900_000_000.0,
    )

    @app.get("/limited", dependencies=[Depends(require_rate_limit(policy))])
    def limited():
        return {"ok": True}

    response = TestClient(app).get(
        "/limited",
        headers={"X-Tenant-ID": "tenant-1", "X-User-ID": "user-1"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    assert response.json()["detail"]["code"] == "rate_limit_exceeded"


def test_rate_limit_dependency_returns_503_when_storage_is_unavailable():
    policy = RateLimitPolicy(name="image_upload", requests_per_minute=10)
    app = FastAPI()
    app.state.rate_limit_service = RateLimitService(
        limiter=FakeLimiter(fail=True),
        fail_closed=True,
    )

    @app.get("/limited", dependencies=[Depends(require_rate_limit(policy))])
    def limited():
        return {"ok": True}

    response = TestClient(app).get(
        "/limited",
        headers={"X-Tenant-ID": "tenant-1", "X-User-ID": "user-1"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "rate_limit_unavailable",
        "message": "请求限流服务暂时不可用。",
    }


def test_rate_limit_factory_disables_service_explicitly():
    service = build_rate_limit_service(
        config=Settings(_env_file=None, rate_limit_backend="none"),
        redis_client=None,
    )

    assert service is None


def test_rate_limit_factory_uses_injected_limiter_for_redis_policy():
    limiter = FakeLimiter()
    service = build_rate_limit_service(
        config=Settings(_env_file=None, rate_limit_backend="redis"),
        redis_client=object(),
        limiter=limiter,
    )

    assert service is not None
    service.consume(
        principal=_principal(),
        policy=RateLimitPolicy(name="agent_invocation", requests_per_minute=20),
    )
    assert limiter.identifiers[0] == "agent_invocation"


def test_production_factory_cannot_enable_fail_open_through_unvalidated_copy():
    runtime = Settings(_env_file=None).model_copy(
        update={
            "app_env": "production",
            "rate_limit_backend": "redis",
            "rate_limit_fail_closed": False,
        }
    )
    service = build_rate_limit_service(
        config=runtime, redis_client=object(), limiter=FakeLimiter(fail=True)
    )
    with pytest.raises(RateLimitUnavailable):
        service.consume(
            principal=_principal(), policy=RateLimitPolicy(name="test", requests_per_minute=1)
        )


@pytest.mark.parametrize("mode", ["production", "configured_local"])
def test_required_service_missing_does_not_execute_endpoint(mode):
    app = FastAPI()
    app.state.config = Settings(_env_file=None).model_copy(
        update={
            "app_env": "production" if mode == "production" else "local",
            "rate_limit_backend": "redis",
        }
    )
    calls = []

    @app.post(
        "/limited",
        dependencies=[
            Depends(require_rate_limit(RateLimitPolicy(name="test", requests_per_minute=1)))
        ],
    )
    def limited():
        calls.append(True)
        return {"ok": True}

    response = TestClient(app).post(
        "/limited",
        json={"rate_limit_backend": "none", "rate_limit_fail_closed": False},
        headers={"X-Tenant-ID": "tenant-1", "X-User-ID": "user-1"},
    )
    assert response.status_code == 503 and not calls
    assert response.json()["detail"]["code"] == "rate_limit_unavailable"


@pytest.mark.parametrize(
    "allowed,stats",
    [
        ("yes", (1900000030.0, 0)),
        (False, (float("nan"), 0)),
        (False, (1900000030.0, "bad")),
        (True, (True, 0)),
        (True, (1900000030.0, -1)),
        (True, (1900000030.0, 2)),
    ],
)
def test_invalid_limiter_results_fail_closed_with_safe_error(allowed, stats):
    class InvalidLimiter:
        def hit(self, *args, **kwargs):
            return allowed

        def get_window_stats(self, *args, **kwargs):
            return stats

    service = RateLimitService(limiter=InvalidLimiter(), now=lambda: 1900000000.0)
    with pytest.raises(RateLimitUnavailable) as error:
        service.consume(
            principal=_principal(), policy=RateLimitPolicy(name="test", requests_per_minute=1)
        )
    assert error.value.__cause__ is None and error.value.__context__ is None


def test_production_factory_rejects_disabled_backend_even_in_unvalidated_copy():
    runtime = Settings(_env_file=None).model_copy(update={"app_env": "production"})
    with pytest.raises(ValueError, match="Redis"):
        build_rate_limit_service(config=runtime, redis_client=None)


def test_explicit_local_disabled_backend_keeps_normal_request_path():
    app = FastAPI()
    app.state.config = Settings(_env_file=None, rate_limit_backend="none")

    @app.get("/limited", dependencies=[Depends(require_rate_limit(RateLimitPolicy("test", 1)))])
    def limited():
        return {"ok": True}

    response = TestClient(app).get("/limited", headers={"X-Tenant-ID": "t", "X-User-ID": "u"})
    assert response.status_code == 200


def test_public_dependency_error_does_not_keep_original_credential_exception():
    app = FastAPI()
    app.state.rate_limit_service = RateLimitService(limiter=FakeLimiter(fail=True))
    from fastapi import HTTPException
    from starlette.requests import Request

    dependency = require_rate_limit(RateLimitPolicy("test", 1))
    with pytest.raises(HTTPException) as error:
        dependency(Request({"type": "http", "app": app}), _principal())
    assert error.value.__cause__ is None and error.value.__context__ is None


@pytest.mark.parametrize("reset,expected", [(1900001000.0, 60), (1899999999.0, 1)])
def test_retry_after_is_bounded_by_one_minute(reset, expected):
    limiter = FakeLimiter(allowed=False)
    limiter.get_window_stats = lambda *args: (reset, 0)
    with pytest.raises(RateLimitExceeded) as error:
        RateLimitService(limiter=limiter, now=lambda: 1900000000.0).consume(
            principal=_principal(),
            policy=RateLimitPolicy("test", 1),
        )
    assert error.value.retry_after_seconds == expected
