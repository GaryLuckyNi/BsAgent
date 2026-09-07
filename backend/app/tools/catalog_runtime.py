from collections.abc import Callable
from threading import Lock

from app.tools.registry import ToolRegistry
from app.tools.search.service import ToolSearchService
from app.tools.search.tool import DeferredToolSearchService, SearchTool


class ToolCatalogRuntime:
    def __init__(
        self,
        *,
        registry_factory: Callable[[], ToolRegistry],
        search_service_factory: Callable[[ToolRegistry], ToolSearchService] | None = None,
    ) -> None:
        self._registry_factory = registry_factory
        self._search_service_factory = search_service_factory
        self._snapshot: ToolRegistry | None = None
        self._search_service: ToolSearchService | None = None
        self._deferred_search_service: DeferredToolSearchService | None = None
        self._last_error: Exception | None = None
        self._lock = Lock()

    def open(self) -> ToolRegistry:
        with self._lock:
            if self._snapshot is not None:
                return self._snapshot
            try:
                registry = self._registry_factory().mutable_copy()
                search_service = None
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
            except Exception as exc:
                self._last_error = exc
                raise
            self._search_service = search_service
            self._deferred_search_service = (
                deferred_service if self._search_service_factory is not None else None
            )
            self._snapshot = snapshot
            self._last_error = None
            return snapshot

    def snapshot(self) -> ToolRegistry:
        return self.open()

    def check_health(self) -> None:
        search_unavailable = (
            self._search_service_factory is not None and self._search_service is None
        )
        if self._snapshot is None or self._last_error is not None or search_unavailable:
            raise RuntimeError("tool_catalog_unavailable")

    @property
    def tool_count(self) -> int:
        return len(self.open().specs())

    @property
    def catalog_digest(self) -> str:
        return self.open().catalog_digest

    @property
    def search_service(self) -> ToolSearchService:
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
