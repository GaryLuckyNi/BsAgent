"""入口预算用真实limits内存后端验证共享/并发，用临时API验证拒绝前无派发。"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from app.api import sessions
from app.approvals.repository import ApprovalRepository
from app.core.config import Settings
from app.operations.rate_limit import RateLimitPolicy, RateLimitService, require_rate_limit
from app.security.dependencies import get_current_principal
from app.security.principal import Principal
from app.tests.integration.test_session_api import create_test_app_with_database
from app.tests.unit.test_rate_limit import FakeLimiter
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter


def limited_app(storage, *, cap=2):
    app = FastAPI()
    app.state.config = Settings(
        _env_file=None, rate_limit_backend="redis", agent_rate_limit_per_minute=cap
    )
    app.state.rate_limit_service = RateLimitService(limiter=FixedWindowRateLimiter(storage))
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "tenant-a", "actor-a", ("agent:invoke",)
    )

    @app.post(
        "/spend", dependencies=[Depends(require_rate_limit(sessions.AGENT_INVOCATION_RATE_LIMIT))]
    )
    def spend():
        return {"ok": True}

    return app


def test_selected_app_config_controls_entry_limit_instead_of_import_time_default():
    storage = MemoryStorage()
    client = TestClient(limited_app(storage, cap=2))
    assert [client.post("/spend").status_code for _ in range(3)] == [200, 200, 429]
    storage.reset()


def test_two_apps_share_the_same_actor_budget_in_the_same_backend():
    storage = MemoryStorage()
    a, b = TestClient(limited_app(storage)), TestClient(limited_app(storage))
    assert a.post("/spend").status_code == 200
    assert b.post("/spend").status_code == 200
    assert a.post("/spend").status_code == b.post("/spend").status_code == 429
    storage.reset()


def test_parallel_requests_do_not_exceed_selected_actor_limit():
    storage = MemoryStorage()
    client = TestClient(limited_app(storage, cap=5))
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: client.post("/spend").status_code, range(24)))
    assert statuses.count(200) == 5 and statuses.count(429) == 19
    storage.reset()


def test_different_tenants_or_users_have_independent_allowances():
    storage = MemoryStorage()
    app = limited_app(storage, cap=1)
    client = TestClient(app)
    assert client.post("/spend").status_code == 200
    assert client.post("/spend").status_code == 429
    for tenant, user in [("tenant-b", "actor-a"), ("tenant-a", "actor-b")]:
        app.dependency_overrides[get_current_principal] = lambda t=tenant, u=user: Principal(
            t, u, ("agent:invoke",)
        )
        assert client.post("/spend").status_code == 200
        assert client.post("/spend").status_code == 429
    storage.reset()


@pytest.mark.parametrize("config_key,cap", [(None, 2), ("image_upload_rate_limit_per_minute", 1)])
def test_static_policies_are_preserved_and_image_policy_uses_its_own_config(config_key, cap):
    storage = MemoryStorage()
    app = limited_app(storage, cap=5)
    app.state.config = app.state.config.model_copy(update={"image_upload_rate_limit_per_minute": 1})
    policy = RateLimitPolicy("image_upload", 2, config_key=config_key)

    @app.post("/image", dependencies=[Depends(require_rate_limit(policy))])
    def image():
        return {"ok": True}

    client = TestClient(app)
    assert [client.post("/image").status_code for _ in range(cap + 1)] == [200] * cap + [429]
    storage.reset()


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/sessions/s1/messages", {"message": "公开问题"}),
        ("/api/sessions/s1/messages/stream", {"message": "公开问题"}),
        (
            "/api/sessions/s1/messages/protocol-stream",
            {
                "input": {
                    "messages": [{"type": "human", "content": "公开问题"}],
                    "request_id": "limit-test",
                }
            },
        ),
        (
            "/api/ag-ui",
            {
                "threadId": "s1",
                "runId": "limit-test",
                "messages": [{"id": "u1", "role": "user", "content": "公开问题"}],
                "tools": [],
                "context": [],
                "forwardedProps": {},
            },
        ),
        ("/api/approvals/no-existing/decision", {"decision": "approved"}),
    ],
)
def test_invocation_routes_do_not_bypass_budget_or_reach_approval_lookup(
    monkeypatch, path, payload
):
    app, _factory = create_test_app_with_database()
    app.state.rate_limit_service = RateLimitService(
        limiter=FakeLimiter(allowed=False), now=lambda: 1900000000.0
    )
    lookups = []
    original = ApprovalRepository.get

    def get(self, **kwargs):
        lookups.append(True)
        return original(self, **kwargs)

    monkeypatch.setattr(ApprovalRepository, "get", get)
    response = TestClient(app).post(path, json={**payload, "user_id": "approver_1"})
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["code"] == "rate_limit_exceeded"
    assert not lookups
