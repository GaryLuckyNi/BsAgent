from collections.abc import Callable
from threading import Lock

from app.tools.registry import ToolRegistry


class ToolCatalogRuntime:
    def __init__(self, *, registry_factory: Callable[[], ToolRegistry]) -> None:
        self._registry_factory = registry_factory
        self._snapshot: ToolRegistry | None = None
        self._last_error: Exception | None = None
        self._lock = Lock()

    def open(self) -> ToolRegistry:
        with self._lock:
            if self._snapshot is not None:
                return self._snapshot
            try:
                snapshot = self._registry_factory().snapshot()
            except Exception as exc:
                self._last_error = exc
                raise
            self._snapshot = snapshot
            self._last_error = None
            return snapshot

    def snapshot(self) -> ToolRegistry:
        return self.open()

    def check_health(self) -> None:
        if self._snapshot is None or self._last_error is not None:
            raise RuntimeError("tool_catalog_unavailable")

    @property
    def tool_count(self) -> int:
        return len(self.open().specs())

    @property
    def catalog_digest(self) -> str:
        return self.open().catalog_digest

    def close(self) -> None:
        with self._lock:
            self._snapshot = None
            self._last_error = None
