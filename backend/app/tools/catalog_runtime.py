import logging
from collections.abc import Callable
from threading import Lock, Thread
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
        self._snapshot_bundle: tuple[ToolRegistry, ToolSearchService | None] | None = None
        self._deferred_search_service: DeferredToolSearchService | None = None
        self._last_error: Exception | None = None
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._last_refresh_at: float | None = None
        self._lock = Lock()
        self._refresh_schedule_lock = Lock()
        self._refresh_thread: Thread | None = None
        self._generation = 0
        self._refresh_requested = False
        self._run_snapshots: dict[str, tuple[ToolRegistry, ToolSearchService | None]] = {}
        self._run_lock = Lock()

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
                self._refresh_requested = False
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

            self._snapshot = next_snapshot
            self._search_service = next_search_service
            self._deferred_search_service = next_deferred_service
            self._last_refresh_at = self._clock()
            self._last_error = None
            self._snapshot_bundle = (next_snapshot, next_search_service)
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
        if self._refresh_requested:
            return True
        if self._refresh_seconds is None:
            return False
        if self._last_refresh_at is None:
            return True
        return self._clock() - self._last_refresh_at >= self._refresh_seconds

    def snapshot(self) -> ToolRegistry:
        return self.open()

    def snapshot_pair_for_run(self, *, run_key: str | None = None) -> tuple[ToolRegistry, ToolSearchService | None]:
        if run_key is not None:
            pinned = self.get_run_snapshot(run_key)
            if pinned is not None:
                # 刷新只发布新快照，不替换该 run 已固定的目录与搜索索引。
                if self._is_expired():
                    self._schedule_background_refresh()
                return pinned
        bundle = self._snapshot_bundle
        if bundle is None:
            self.open()
            bundle = self._snapshot_bundle
        if bundle is None:
            raise RuntimeError("tool_catalog_unavailable")
        if self._is_expired():
            self._schedule_background_refresh()
        if run_key is not None:
            with self._run_lock:
                return self._run_snapshots.setdefault(run_key, bundle)
        return bundle

    def retain_run_snapshot(self, run_key: str, bundle: tuple[ToolRegistry, ToolSearchService | None]) -> None:
        with self._run_lock:
            existing = self._run_snapshots.setdefault(run_key, bundle)
            if existing[0].catalog_digest != bundle[0].catalog_digest:
                raise RuntimeError("run_tool_catalog_mismatch")

    def get_run_snapshot(self, run_key: str) -> tuple[ToolRegistry, ToolSearchService | None] | None:
        with self._run_lock:
            return self._run_snapshots.get(run_key)

    def release_run_snapshot(self, run_key: str) -> None:
        with self._run_lock:
            self._run_snapshots.pop(run_key, None)

    def request_refresh(self) -> None:
        """配置变化或工具列表通知触发发现；进行中的 run 继续持有原快照。"""
        self._refresh_requested = True
        self._schedule_background_refresh()

    def _schedule_background_refresh(self) -> None:
        with self._refresh_schedule_lock:
            if self._refresh_thread is not None and self._refresh_thread.is_alive():
                return
            generation = self._generation
            thread = Thread(
                target=self._refresh_in_background,
                args=(generation,),
                name="tool-catalog-refresh",
                daemon=True,
            )
            self._refresh_thread = thread
            thread.start()

    def _refresh_in_background(self, generation: int) -> None:
        if generation != self._generation:
            return
        try:
            self.open()
        except Exception as exc:  # noqa: BLE001 - 后台发现失败保留现有快照
            logger.warning("tool_catalog_background_refresh_failed type=%s", type(exc).__name__)

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
        with self._refresh_schedule_lock:
            self._generation += 1
            refresh_thread = self._refresh_thread
        if refresh_thread is not None and refresh_thread.is_alive():
            refresh_thread.join()
        with self._lock:
            if self._deferred_search_service is not None:
                self._deferred_search_service.close()
            self._snapshot = None
            self._search_service = None
            self._snapshot_bundle = None
            self._deferred_search_service = None
            self._last_error = None
            self._last_refresh_at = None
            self._refresh_thread = None
            self._refresh_requested = False
            with self._run_lock:
                self._run_snapshots.clear()
