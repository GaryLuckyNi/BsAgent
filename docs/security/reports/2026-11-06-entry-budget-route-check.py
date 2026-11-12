"""隔离复跑跨实际入口共享额度与 AG-UI 不双扣，不调用真实模型/Redis。"""

from app.api import sessions
from app.core.config import Settings
from app.operations.rate_limit import RateLimitService
from app.security.dependencies import get_current_principal
from app.security.principal import Principal
from app.tests.integration.test_session_api import create_test_app_with_database
from fastapi.testclient import TestClient
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter


def source(**kwargs):
    yield sessions._sse("status", {"message_id": "assistant-local"})
    yield sessions._sse("final", {
        "content": "公开正常回答", "trace_id": "trace-local",
        "cited_evidence_ids": [], "approval_status": "not_required",
    })


def main():
    app, factory = create_test_app_with_database()
    app.state.config = Settings(
        _env_file=None, rate_limit_backend="redis", agent_rate_limit_per_minute=2,
    )
    storage = MemoryStorage()
    app.state.rate_limit_service = RateLimitService(limiter=FixedWindowRateLimiter(storage))
    app.dependency_overrides[get_current_principal] = lambda: Principal("tenant_1", "user_1", ("*",))
    original = sessions._stream_agent_message
    sessions._stream_agent_message = source
    try:
        client = TestClient(app)
        payload = {
            "threadId": "s1", "runId": "local-budget",
            "messages": [{"id": "u1", "role": "user", "content": "公开问题"}],
            "tools": [], "context": [], "forwardedProps": {},
        }
        results = [
            client.post("/api/ag-ui", json=payload).status_code,
            client.post("/api/approvals/no-existing/decision", json={"decision": "approved"}).status_code,
            client.post("/api/ag-ui", json=payload).status_code,
        ]
        print(results)
        assert results == [200, 404, 429]
    finally:
        sessions._stream_agent_message = original
        storage.reset()
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    main()
