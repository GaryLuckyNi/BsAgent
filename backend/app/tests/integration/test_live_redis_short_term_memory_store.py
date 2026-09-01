import os

import pytest

from app.core.config import settings
from app.memory.short_term_store import RedisShortTermMemoryStore


pytestmark = pytest.mark.skipif(
    os.getenv("BS_AGENT_RUN_LIVE_REDIS_TESTS") != "true",
    reason="需要显式设置 BS_AGENT_RUN_LIVE_REDIS_TESTS=true 才访问本地 Redis。",
)


def test_live_redis_short_term_memory_store_smoke_round_trip():
    store = RedisShortTermMemoryStore.from_url(
        settings.redis_url,
        key_prefix="bsagent:test",
        default_ttl_seconds=60,
        max_events_per_session=2,
    )
    store.delete_session(tenant_id="tenant_smoke", session_id="session_smoke")

    store.set_session_value(
        tenant_id="tenant_smoke",
        session_id="session_smoke",
        key="scratch",
        value={"value": "ok"},
    )
    store.append_session_event(
        tenant_id="tenant_smoke",
        session_id="session_smoke",
        event={"node": "skill_agent"},
    )

    assert store.get_session_value(
        tenant_id="tenant_smoke",
        session_id="session_smoke",
        key="scratch",
    ) == {"value": "ok"}
    assert store.list_session_events(
        tenant_id="tenant_smoke",
        session_id="session_smoke",
    ) == [{"node": "skill_agent"}]

    assert store.delete_session(tenant_id="tenant_smoke", session_id="session_smoke") == 2
