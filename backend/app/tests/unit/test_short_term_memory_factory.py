from app.memory.short_term_factory import build_short_term_memory_store
from app.memory.short_term_store import NoopShortTermMemoryStore, RedisShortTermMemoryStore


class FakeSettings:
    def __init__(
        self,
        *,
        short_term_memory_backend: str = "none",
        redis_url: str = "redis://localhost:6379/0",
        short_term_memory_ttl_seconds: int = 300,
        short_term_memory_max_events: int = 20,
    ) -> None:
        self.short_term_memory_backend = short_term_memory_backend
        self.redis_url = redis_url
        self.short_term_memory_ttl_seconds = short_term_memory_ttl_seconds
        self.short_term_memory_max_events = short_term_memory_max_events


class FakeRedisClient:
    pass


def test_build_short_term_memory_store_defaults_to_noop_store():
    store = build_short_term_memory_store(settings=FakeSettings())

    assert isinstance(store, NoopShortTermMemoryStore)


def test_build_short_term_memory_store_can_use_injected_redis_client():
    store = build_short_term_memory_store(
        settings=FakeSettings(short_term_memory_backend="redis"),
        redis_client=FakeRedisClient(),
    )

    assert isinstance(store, RedisShortTermMemoryStore)


def test_build_short_term_memory_store_rejects_unknown_backend():
    try:
        build_short_term_memory_store(
            settings=FakeSettings(short_term_memory_backend="postgres"),
        )
    except ValueError as exc:
        assert "未知短期记忆后端" in str(exc)
    else:
        raise AssertionError("未知短期记忆后端必须显式失败")
