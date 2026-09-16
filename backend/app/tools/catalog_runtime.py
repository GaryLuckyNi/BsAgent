from collections.abc import Callable
import logging
from threading import Lock
from time import monotonic

from app.tools.registry import ToolRegistry
from app.tools.search.service import ToolSearchService
from app.tools.search.tool import DeferredToolSearchService, SearchTool


logger = logging.getLogger(__name__)


class ToolCatalogRuntime:
    def __init__(
        self,
        *,
        registry_factory: Callable[[], ToolRegistry],
        search_service_factory: Callable[[ToolRegistry], ToolSearchService] | None = None,
        refresh_seconds: float | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._registry_factory = registry_factory
        self._search_service_factory = search_service_factory
        self._snapshot: ToolRegistry | None = None
        self._search_service: ToolSearchService | None = None
        self._deferred_search_service: DeferredToolSearchService | None = None
        self._last_error: Exception | None = None
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._last_refresh_at: float | None = None
        self._lock = Lock()

    def open(self) -> ToolRegistry:
        snapshot = self._snapshot
        if snapshot is not None and not self._is_expired():
            return snapshot

        if snapshot is None:
            self._lock.acquire()
        elif not self._lock.acquire(blocking=False):
            return snapshot

        try:
            snapshot = self._snapshot
            if snapshot is not None and not self._is_expired():
                return snapshot
            try:
                (
                    next_snapshot,
                    next_search_service,
                    next_deferred_service,
                ) = self._build_snapshot()
            except Exception as exc:
                self._last_error = exc
                self._last_refresh_at = self._clock()
                if snapshot is not None:
                    logger.warning(
                        "tool_catalog_refresh_failed type=%s",
                        type(exc).__name__,
                    )
                    return snapshot
                raise

            previous_deferred_service = self._deferred_search_service
            self._snapshot = next_snapshot
            self._search_service = next_search_service
            self._deferred_search_service = next_deferred_service
            self._last_refresh_at = self._clock()
            self._last_error = None
            if previous_deferred_service is not None:
                previous_deferred_service.close()
            return next_snapshot
        finally:
            self._lock.release()

    def _build_snapshot(
        self,
    ) -> tuple[
        ToolRegistry,
        ToolSearchService | None,
        DeferredToolSearchService | None,
    ]:
        registry = self._registry_factory().mutable_copy()
        search_service = None
        deferred_service = None
        if self._search_service_factory is not None:
            deferred_service = DeferredToolSearchService()
            registry.register(SearchTool(deferred_service))
            snapshot = registry.snapshot()
            search_service = self._search_service_factory(snapshot)
            if search_service.catalog_digest != snapshot.catalog_digest:
                raise ValueError("工具目录与搜索服务 digest 不一致。")
            deferred_service.bind(search_service)
        else:
            snapshot = registry.snapshot()
        return snapshot, search_service, deferred_service

    def _is_expired(self) -> bool:
        if self._refresh_seconds is None:
            return False
        if self._last_refresh_at is None:
            return True
        return self._clock() - self._last_refresh_at >= self._refresh_seconds

    def snapshot(self) -> ToolRegistry:
        return self.open()

    def check_health(self) -> None:
        search_unavailable = (
            self._search_service_factory is not None and self._search_service is None
        )
        if self._snapshot is None or search_unavailable:
            raise RuntimeError("tool_catalog_unavailable")

    @property
    def tool_count(self) -> int:
        return len(self.open().specs())

    @property
    def catalog_digest(self) -> str:
        return self.open().catalog_digest

    @property
    def search_service(self) -> ToolSearchService:
        if self._snapshot is not None:
            self.open()
        if self._search_service is None:
            raise RuntimeError("tool_search_unavailable")
        return self._search_service

    def close(self) -> None:
        with self._lock:
            if self._deferred_search_service is not None:
                self._deferred_search_service.close()
            self._snapshot = None
            self._search_service = None
            self._deferred_search_service = None
            self._last_error = None
            self._last_refresh_at = None
