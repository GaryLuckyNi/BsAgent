from app.memory.short_term_store import RedisShortTermMemoryStore


class FakeRedisClient:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.expires: dict[str, int] = {}

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        self.values[key] = value
        self.expires[key] = ttl_seconds

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    def ltrim(self, key: str, start: int, end: int) -> None:
        values = self.lists.get(key, [])
        normalized_start = len(values) + start if start < 0 else start
        normalized_end = len(values) + end if end < 0 else end
        self.lists[key] = values[normalized_start : normalized_end + 1]

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self.lists.get(key, [])
        if end == -1:
            return values[start:]
        return values[start : end + 1]

    def expire(self, key: str, ttl_seconds: int) -> None:
        self.expires[key] = ttl_seconds

    def scan_iter(self, match: str):
        prefix = match.removesuffix("*")
        keys = set(self.values) | set(self.lists)
        for key in sorted(keys):
            if key.startswith(prefix):
                yield key

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.values:
                deleted += 1
                del self.values[key]
            if key in self.lists:
                deleted += 1
                del self.lists[key]
            self.expires.pop(key, None)
        return deleted


def test_redis_short_term_memory_store_round_trips_session_value_with_ttl():
    client = FakeRedisClient()
    store = RedisShortTermMemoryStore(
        client=client,
        key_prefix="test",
        default_ttl_seconds=60,
    )

    store.set_session_value(
        tenant_id="tenant_1",
        session_id="session_1",
        key="tool_cache",
        value={"answer": "ok", "score": 0.9},
    )

    assert store.get_session_value(
        tenant_id="tenant_1",
        session_id="session_1",
        key="tool_cache",
    ) == {"answer": "ok", "score": 0.9}
    assert client.expires["test:short_term:tenant_1:session_1:kv:tool_cache"] == 60


def test_redis_short_term_memory_store_keeps_recent_events_in_order():
    client = FakeRedisClient()
    store = RedisShortTermMemoryStore(
        client=client,
        key_prefix="test",
        default_ttl_seconds=120,
        max_events_per_session=2,
    )

    store.append_session_event(
        tenant_id="tenant_1",
        session_id="session_1",
        event={"node": "intent_router"},
    )
    store.append_session_event(
        tenant_id="tenant_1",
        session_id="session_1",
        event={"node": "planner_agent"},
    )
    store.append_session_event(
        tenant_id="tenant_1",
        session_id="session_1",
        event={"node": "skill_agent"},
    )

    assert store.list_session_events(
        tenant_id="tenant_1",
        session_id="session_1",
    ) == [{"node": "planner_agent"}, {"node": "skill_agent"}]
    assert client.expires["test:short_term:tenant_1:session_1:events"] == 120


def test_redis_short_term_memory_store_deletes_session_hot_data():
    client = FakeRedisClient()
    store = RedisShortTermMemoryStore(client=client, key_prefix="test")
    store.set_session_value(
        tenant_id="tenant_1",
        session_id="session_1",
        key="scratch",
        value={"value": 1},
    )
    store.append_session_event(
        tenant_id="tenant_1",
        session_id="session_1",
        event={"node": "memory_manager"},
    )

    deleted = store.delete_session(tenant_id="tenant_1", session_id="session_1")

    assert deleted == 2
    assert store.get_session_value(
        tenant_id="tenant_1",
        session_id="session_1",
        key="scratch",
    ) is None
    assert store.list_session_events(tenant_id="tenant_1", session_id="session_1") == []
