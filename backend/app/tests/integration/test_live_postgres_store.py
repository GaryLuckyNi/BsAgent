from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.tests.support.postgres_store import LivePostgresStore


pytest_plugins = ("app.tests.support.postgres_store",)


def _namespace(tenant_id: str, user_id: str) -> tuple[str, ...]:
    return ("tenants", tenant_id, "users", user_id, "experiences")


def test_postgres_store_setup_supports_put_search_and_delete(
    live_postgres_store: LivePostgresStore,
):
    namespace = _namespace("tenant-a", "user-a")

    live_postgres_store.store.put(
        namespace,
        "memory-1",
        {"content": "first memory"},
    )

    items = live_postgres_store.store.search(namespace)
    assert [(item.key, item.value) for item in items] == [
        ("memory-1", {"content": "first memory"})
    ]

    live_postgres_store.store.delete(namespace, "memory-1")
    assert live_postgres_store.store.search(namespace) == []


def test_postgres_store_isolates_tenant_and_user_namespaces(
    live_postgres_store: LivePostgresStore,
):
    expected = {
        _namespace("tenant-a", "user-a"): "tenant-a-user-a",
        _namespace("tenant-a", "user-b"): "tenant-a-user-b",
        _namespace("tenant-b", "user-a"): "tenant-b-user-a",
    }
    for namespace, content in expected.items():
        live_postgres_store.store.put(
            namespace,
            "shared-key",
            {"content": content},
        )

    for namespace, content in expected.items():
        items = live_postgres_store.store.search(namespace)
        assert [(item.key, item.value["content"]) for item in items] == [
            ("shared-key", content)
        ]


def test_postgres_store_replaces_value_for_same_memory_key(
    live_postgres_store: LivePostgresStore,
):
    namespace = _namespace("tenant-a", "user-a")

    live_postgres_store.store.put(namespace, "stable-key", {"version": 1})
    live_postgres_store.store.put(namespace, "stable-key", {"version": 2})

    items = live_postgres_store.store.search(namespace)
    assert [(item.key, item.value) for item in items] == [
        ("stable-key", {"version": 2})
    ]


def test_postgres_store_runtime_reuses_and_reopens_live_store(
    live_postgres_store: LivePostgresStore,
):
    from app.memory.runtime import PostgresStoreRuntime

    runtime = PostgresStoreRuntime(
        settings=SimpleNamespace(
            experience_store_postgres_url=live_postgres_store.dsn
        )
    )
    namespace = _namespace("runtime-tenant", "runtime-user")

    first = runtime.open()
    try:
        assert runtime.open() is first
        first.put(namespace, "runtime-key", {"connected": True})
        runtime.check_health()

        runtime.close()
        runtime.close()

        reopened = runtime.open()
        assert reopened is not first
        item = reopened.get(namespace, "runtime-key")
        assert item is not None
        assert item.value == {"connected": True}
    finally:
        runtime.close()
