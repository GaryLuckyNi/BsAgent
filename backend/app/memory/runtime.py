import sys
from contextlib import AbstractContextManager
from threading import Lock

from langgraph.store.base import BaseStore
from langgraph.store.postgres import PostgresStore

from app.core.config import Settings, settings


class PostgresStoreRuntimeError(RuntimeError):
    pass


class PostgresStoreRuntime:
    def __init__(
        self,
        *,
        settings: Settings = settings,
    ) -> None:
        self._settings = settings
        self._context: AbstractContextManager[BaseStore] | None = None
        self._store: BaseStore | None = None
        self._lock = Lock()

    def open(self) -> BaseStore:
        with self._lock:
            if self._store is not None:
                return self._store
            url = self._settings.experience_store_postgres_url
            if not url:
                raise PostgresStoreRuntimeError(
                    "LangGraph PostgresStore requires experience_store_postgres_url."
                )
            context = PostgresStore.from_conn_string(
                url,
                pool_config={"min_size": 1, "max_size": 10},
            )
            store = context.__enter__()
            try:
                store.setup()
            except Exception:
                context.__exit__(*sys.exc_info())
                raise
            self._context = context
            self._store = store
            return store

    def get(self) -> BaseStore:
        return self.open()

    def check_health(self) -> None:
        self.open().search(("health",), limit=1)

    def close(self) -> None:
        with self._lock:
            context = self._context
            self._context = None
            self._store = None
        if context is not None:
            context.__exit__(None, None, None)
