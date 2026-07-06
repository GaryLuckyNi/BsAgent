import json
from typing import Any, Protocol


class ShortTermMemoryStore(Protocol):
    def set_session_value(
        self,
        *,
        tenant_id: str,
        session_id: str,
        key: str,
        value: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        raise NotImplementedError

    def get_session_value(
        self,
        *,
        tenant_id: str,
        session_id: str,
        key: str,
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    def append_session_event(
        self,
        *,
        tenant_id: str,
        session_id: str,
        event: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        raise NotImplementedError

    def list_session_events(
        self,
        *,
        tenant_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def delete_session(self, *, tenant_id: str, session_id: str) -> int:
        raise NotImplementedError


class RedisShortTermMemoryStore:
    def __init__(
        self,
        *,
        client: Any,
        key_prefix: str = "bsagent",
        default_ttl_seconds: int = 3600,
        max_events_per_session: int = 100,
    ) -> None:
        self._client = client
        self._key_prefix = key_prefix
        self._default_ttl_seconds = default_ttl_seconds
        self._max_events_per_session = max_events_per_session

    @classmethod
    def from_url(
        cls,
        redis_url: str,
        *,
        key_prefix: str = "bsagent",
        default_ttl_seconds: int = 3600,
        max_events_per_session: int = 100,
    ) -> "RedisShortTermMemoryStore":
        from redis import Redis

        return cls(
            client=Redis.from_url(redis_url, decode_responses=True),
            key_prefix=key_prefix,
            default_ttl_seconds=default_ttl_seconds,
            max_events_per_session=max_events_per_session,
        )

    def set_session_value(
        self,
        *,
        tenant_id: str,
        session_id: str,
        key: str,
        value: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        redis_key = self._value_key(tenant_id, session_id, key)
        encoded = _encode(value)
        selected_ttl = ttl_seconds or self._default_ttl_seconds
        if selected_ttl > 0:
            self._client.setex(redis_key, selected_ttl, encoded)
            return
        self._client.set(redis_key, encoded)

    def get_session_value(
        self,
        *,
        tenant_id: str,
        session_id: str,
        key: str,
    ) -> dict[str, Any] | None:
        raw_value = self._client.get(self._value_key(tenant_id, session_id, key))
        if raw_value is None:
            return None
        return _decode_json(raw_value)

    def append_session_event(
        self,
        *,
        tenant_id: str,
        session_id: str,
        event: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        redis_key = self._events_key(tenant_id, session_id)
        self._client.rpush(redis_key, _encode(event))
        self._client.ltrim(redis_key, -self._max_events_per_session, -1)
        selected_ttl = ttl_seconds or self._default_ttl_seconds
        if selected_ttl > 0:
            self._client.expire(redis_key, selected_ttl)

    def list_session_events(
        self,
        *,
        tenant_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        values = self._client.lrange(self._events_key(tenant_id, session_id), 0, -1)
        return [_decode_json(value) for value in values]

    def delete_session(self, *, tenant_id: str, session_id: str) -> int:
        pattern = f"{self._session_prefix(tenant_id, session_id)}*"
        keys = list(self._client.scan_iter(match=pattern))
        if not keys:
            return 0
        return int(self._client.delete(*keys))

    def _value_key(self, tenant_id: str, session_id: str, key: str) -> str:
        return f"{self._session_prefix(tenant_id, session_id)}kv:{key}"

    def _events_key(self, tenant_id: str, session_id: str) -> str:
        return f"{self._session_prefix(tenant_id, session_id)}events"

    def _session_prefix(self, tenant_id: str, session_id: str) -> str:
        return f"{self._key_prefix}:short_term:{tenant_id}:{session_id}:"


class NoopShortTermMemoryStore:
    def set_session_value(
        self,
        *,
        tenant_id: str,
        session_id: str,
        key: str,
        value: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        return None

    def get_session_value(
        self,
        *,
        tenant_id: str,
        session_id: str,
        key: str,
    ) -> dict[str, Any] | None:
        return None

    def append_session_event(
        self,
        *,
        tenant_id: str,
        session_id: str,
        event: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        return None

    def list_session_events(
        self,
        *,
        tenant_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return []

    def delete_session(self, *, tenant_id: str, session_id: str) -> int:
        return 0


def _encode(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise ValueError("短期记忆值必须是 JSON object")
    return decoded
