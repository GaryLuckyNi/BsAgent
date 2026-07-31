from functools import lru_cache
import importlib
import logging
from pathlib import Path
import sqlite3
from threading import Lock
from collections.abc import Callable
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Checkpointer

from app.core.config import settings

CheckpointBackend = Literal["memory", "none", "sqlite", "postgres"]
PROJECT_ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger(__name__)
_resource_lock = Lock()
_resource_closers: list[Callable[[], None]] = []


class CheckpointBackendError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _build_cached_graph_checkpointer(
    backend: CheckpointBackend,
    sqlite_path: str,
    postgres_url: str | None,
) -> Checkpointer:
    if backend == "none":
        return None
    if backend == "memory":
        return InMemorySaver()
    if backend == "sqlite":
        return _build_sqlite_checkpointer(sqlite_path)
    if backend == "postgres":
        if not postgres_url:
            raise CheckpointBackendError(
                "PostgreSQL graph checkpointing requires postgres_url."
            )
        return _build_postgres_checkpointer(postgres_url)
    raise CheckpointBackendError(f"Unsupported graph checkpointer backend: {backend}")


def build_graph_checkpointer(
    *,
    backend: CheckpointBackend | None = None,
    sqlite_path: str | None = None,
    postgres_url: str | None = None,
) -> Checkpointer:
    return _build_cached_graph_checkpointer(
        backend or settings.graph_checkpointer_backend,
        sqlite_path or settings.graph_checkpoint_sqlite_path,
        postgres_url or settings.graph_checkpoint_postgres_url,
    )


def _build_sqlite_checkpointer(sqlite_path: str) -> Checkpointer:
    try:
        sqlite_module = importlib.import_module("langgraph.checkpoint.sqlite")
    except ModuleNotFoundError as exc:
        raise CheckpointBackendError(
            "SQLite graph checkpointing requires the official "
            "`langgraph-checkpoint-sqlite` package."
        ) from exc

    resolved_path = _resolve_sqlite_path(sqlite_path)
    if resolved_path != ":memory:":
        Path(resolved_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = sqlite_module.SqliteSaver(conn)
    try:
        saver.setup()
    except Exception:
        conn.close()
        raise
    _register_resource_closer(conn.close)
    return saver


def _build_postgres_checkpointer(postgres_url: str) -> Checkpointer:
    try:
        postgres_module = importlib.import_module("langgraph.checkpoint.postgres")
        pool_module = importlib.import_module("psycopg_pool")
    except ModuleNotFoundError as exc:
        raise CheckpointBackendError(
            "PostgreSQL graph checkpointing requires "
            "`langgraph-checkpoint-postgres` and `psycopg[pool]`."
        ) from exc

    pool = pool_module.ConnectionPool(
        postgres_url,
        min_size=1,
        max_size=10,
        open=True,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    saver = postgres_module.PostgresSaver(pool)
    try:
        saver.setup()
    except Exception:
        pool.close()
        raise
    _register_resource_closer(pool.close)
    return saver


def _resolve_sqlite_path(sqlite_path: str) -> str:
    if sqlite_path == ":memory:":
        return sqlite_path
    path = Path(sqlite_path)
    if path.is_absolute():
        return str(path)
    return str(PROJECT_ROOT / path)


def close_graph_checkpointer() -> None:
    with _resource_lock:
        closers = list(reversed(_resource_closers))
        _resource_closers.clear()
    for close in closers:
        try:
            close()
        except Exception:
            logger.exception("graph_checkpointer_close_failed")
    _build_cached_graph_checkpointer.cache_clear()


def _register_resource_closer(close: Callable[[], None]) -> None:
    with _resource_lock:
        _resource_closers.append(close)


build_graph_checkpointer.cache_clear = close_graph_checkpointer  # type: ignore[attr-defined]
