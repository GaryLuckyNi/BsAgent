from functools import lru_cache
import importlib
import logging
from threading import Lock
from collections.abc import Callable

from langgraph.types import Checkpointer

from app.core.config import settings

logger = logging.getLogger(__name__)
_resource_lock = Lock()
_resource_closers: list[Callable[[], None]] = []


class CheckpointBackendError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _build_cached_graph_checkpointer(
    postgres_url: str | None,
) -> Checkpointer:
    if not postgres_url:
        raise CheckpointBackendError(
            "PostgreSQL graph checkpointing requires postgres_url."
        )
    return _build_postgres_checkpointer(postgres_url)


def build_graph_checkpointer(
    *,
    postgres_url: str | None = None,
) -> Checkpointer:
    return _build_cached_graph_checkpointer(
        postgres_url or settings.graph_checkpoint_postgres_url,
    )


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
