from functools import lru_cache
import importlib
from pathlib import Path
import sqlite3
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Checkpointer

from app.core.config import settings

CheckpointBackend = Literal["memory", "none", "sqlite"]
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class CheckpointBackendError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _build_cached_graph_checkpointer(
    backend: CheckpointBackend,
    sqlite_path: str,
) -> Checkpointer:
    if backend == "none":
        return None
    if backend == "memory":
        return InMemorySaver()
    if backend == "sqlite":
        return _build_sqlite_checkpointer(sqlite_path)
    raise CheckpointBackendError(f"Unsupported graph checkpointer backend: {backend}")


def build_graph_checkpointer(
    *,
    backend: CheckpointBackend | None = None,
    sqlite_path: str | None = None,
) -> Checkpointer:
    return _build_cached_graph_checkpointer(
        backend or settings.graph_checkpointer_backend,
        sqlite_path or settings.graph_checkpoint_sqlite_path,
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
    saver.setup()
    return saver


def _resolve_sqlite_path(sqlite_path: str) -> str:
    if sqlite_path == ":memory:":
        return sqlite_path
    path = Path(sqlite_path)
    if path.is_absolute():
        return str(path)
    return str(PROJECT_ROOT / path)


build_graph_checkpointer.cache_clear = _build_cached_graph_checkpointer.cache_clear  # type: ignore[attr-defined]
