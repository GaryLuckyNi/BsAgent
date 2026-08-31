from __future__ import annotations

import os
from threading import Event, Thread
from types import SimpleNamespace
from typing import TYPE_CHECKING

import psycopg
import pytest
from langgraph.store.postgres import PostgresStore

if TYPE_CHECKING:
    from app.tests.support.postgres_store import LivePostgresStore


pytest_plugins = ("app.tests.support.postgres_store",)


def _namespace(tenant_id: str, user_id: str) -> tuple[str, ...]:
    return ("tenants", tenant_id, "users", user_id, "experiences")


def test_postgres_store_support_drops_schema_when_dsn_build_fails(monkeypatch):
    from app.tests.support import postgres_store

    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("未配置真实 PostgreSQL Store 测试地址。")

    created_schema: dict[str, str] = {}

    def fail_dsn_build(_postgres_url: str, schema_name: str) -> str:
        created_schema["name"] = schema_name
        raise RuntimeError("forced DSN build failure")

    monkeypatch.setattr(
        postgres_store,
        "_build_isolated_dsn",
        fail_dsn_build,
    )

    with pytest.raises(RuntimeError, match="forced DSN build failure"):
        with postgres_store.open_isolated_postgres_store(postgres_url):
            pass

    with psycopg.connect(postgres_url) as connection:
        row = connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname = %s)",
            (created_schema["name"],),
        ).fetchone()
    assert row is not None
    assert row[0] is False


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


def test_postgres_store_runtime_serializes_close_and_open(
    live_postgres_store: LivePostgresStore,
    monkeypatch,
):
    from app.memory.runtime import PostgresStoreRuntime

    runtime = PostgresStoreRuntime(
        settings=SimpleNamespace(
            experience_store_postgres_url=live_postgres_store.dsn
        )
    )
    first = runtime.open()
    assert isinstance(first, PostgresStore)
    first_context = runtime._context
    assert first_context is not None

    context_type = type(first_context)
    original_exit = context_type.__exit__
    original_from_conn_string = PostgresStore.from_conn_string
    exit_started = Event()
    allow_exit = Event()
    open_attempted = Event()
    reopen_started = Event()
    close_errors: list[BaseException] = []
    open_errors: list[BaseException] = []

    def blocking_exit(context, *args):
        if context is first_context:
            exit_started.set()
            if not allow_exit.wait(timeout=5):
                raise TimeoutError("timed out waiting to close PostgresStore")
        return original_exit(context, *args)

    def tracked_from_conn_string(*args, **kwargs):
        reopen_started.set()
        return original_from_conn_string(*args, **kwargs)

    def close_runtime() -> None:
        try:
            runtime.close()
        except BaseException as exc:
            close_errors.append(exc)

    def open_runtime() -> None:
        open_attempted.set()
        try:
            runtime.open()
        except BaseException as exc:
            open_errors.append(exc)

    monkeypatch.setattr(context_type, "__exit__", blocking_exit)
    monkeypatch.setattr(
        PostgresStore,
        "from_conn_string",
        tracked_from_conn_string,
    )

    close_thread = Thread(target=close_runtime)
    open_thread = Thread(target=open_runtime)
    close_thread.start()
    try:
        assert exit_started.wait(timeout=5)
        open_thread.start()
        assert open_attempted.wait(timeout=5)
        reopened_before_close_finished = reopen_started.wait(timeout=0.5)
    finally:
        allow_exit.set()
        close_thread.join(timeout=5)
        if open_thread.ident is not None:
            open_thread.join(timeout=5)

    try:
        assert not close_thread.is_alive()
        assert not open_thread.is_alive()
        assert close_errors == []
        assert open_errors == []
        assert reopened_before_close_finished is False
    finally:
        runtime.close()
