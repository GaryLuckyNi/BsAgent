from typing import Any

from app.core.config import Settings, settings
from app.memory.short_term_store import (
    NoopShortTermMemoryStore,
    RedisShortTermMemoryStore,
    ShortTermMemoryStore,
)


def build_short_term_memory_store(
    *,
    settings: Settings = settings,
    redis_client: Any | None = None,
) -> ShortTermMemoryStore:
    backend = settings.short_term_memory_backend
    if backend == "none":
        return NoopShortTermMemoryStore()
    if backend == "redis":
        if redis_client is not None:
            return RedisShortTermMemoryStore(
                client=redis_client,
                default_ttl_seconds=settings.short_term_memory_ttl_seconds,
                max_events_per_session=settings.short_term_memory_max_events,
            )
        return RedisShortTermMemoryStore.from_url(
            settings.redis_url,
            default_ttl_seconds=settings.short_term_memory_ttl_seconds,
            max_events_per_session=settings.short_term_memory_max_events,
        )
    raise ValueError(f"未知短期记忆后端: {backend}")
