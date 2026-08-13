import importlib
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from threading import Lock
from typing import Any

from langgraph.store.base import BaseStore

from app.core.config import Settings, settings


class ExperienceStoreRuntimeError(RuntimeError):
    pass


class ExperienceStoreRuntime:
    def __init__(
        self,
        *,
        settings: Settings = settings,
        factory: Callable[..., AbstractContextManager[BaseStore]] | None = None,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._context: AbstractContextManager[BaseStore] | None = None
        self._store: BaseStore | None = None
        self._lock = Lock()

    def open(self) -> BaseStore | None:
        if self._settings.experience_store_backend == "sqlalchemy":
            return None
        with self._lock:
            if self._store is not None:
                return self._store
            url = self._settings.experience_store_postgres_url
            if not url:
                raise ExperienceStoreRuntimeError(
                    "LangGraph PostgresStore requires experience_store_postgres_url."
                )
            factory = self._factory or _postgres_store_factory()
            context = factory(
                url,
                pool_config={"min_size": 1, "max_size": 10},
            )
            try:
                store = context.__enter__()
                setup = getattr(store, "setup", None)
                if setup is not None:
                    setup()
            except Exception:
                context.__exit__(*sys.exc_info())
                raise
            self._context = context
            self._store = store
            return store

    def get(self) -> BaseStore | None:
        return self.open()

    def check_health(self) -> None:
        store = self.open()
        if store is not None:
            store.search(("health",), limit=1)

    def close(self) -> None:
        with self._lock:
            context = self._context
            self._context = None
            self._store = None
        if context is not None:
            context.__exit__(None, None, None)


def _postgres_store_factory() -> Callable[..., Any]:
    try:
        module = importlib.import_module("langgraph.store.postgres")
    except ModuleNotFoundError as exc:
        raise ExperienceStoreRuntimeError(
            "PostgresStore requires the official langgraph-checkpoint-postgres package."
        ) from exc
    return module.PostgresStore.from_conn_string
