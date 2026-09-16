import pytest

from app.tools.catalog_runtime import ToolCatalogRuntime
from app.tools.search import ToolSearchContext, ToolSearchInput, ToolSearchResponse
from app.tools.registry import ToolRegistry


class _SearchService:
    def __init__(self, catalog_digest):
        self.catalog_digest = catalog_digest

    def search(self, request, *, context):
        return ToolSearchResponse(
            catalog_digest=self.catalog_digest,
            mode="hybrid",
        )


def test_tool_catalog_runtime_discovers_once_and_returns_frozen_snapshot():
    calls = []

    def discover():
        calls.append("discover")
        return ToolRegistry()

    runtime = ToolCatalogRuntime(registry_factory=discover)

    first = runtime.open()
    second = runtime.open()

    assert first is second
    assert calls == ["discover"]
    assert runtime.tool_count == 0
    assert runtime.catalog_digest == first.catalog_digest


def test_tool_catalog_runtime_health_requires_successful_discovery():
    runtime = ToolCatalogRuntime(
        registry_factory=lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    )

    try:
        runtime.open()
    except RuntimeError:
        pass

    try:
        runtime.check_health()
    except RuntimeError as exc:
        assert str(exc) == "tool_catalog_unavailable"
    else:
        raise AssertionError("未发现工具时 readiness 必须失败")


def test_tool_catalog_runtime_builds_search_once_from_final_snapshot():
    build_digests = []

    def build_search(registry):
        assert registry.has("search_tool")
        build_digests.append(registry.catalog_digest)
        return _SearchService(registry.catalog_digest)

    runtime = ToolCatalogRuntime(
        registry_factory=ToolRegistry,
        search_service_factory=build_search,
    )

    first = runtime.open()
    second = runtime.open()

    assert first is second
    assert first.has("search_tool")
    assert build_digests == [first.catalog_digest]
    assert runtime.search_service.catalog_digest == first.catalog_digest
    response = runtime.search_service.search(
        ToolSearchInput(query="利润表"),
        context=ToolSearchContext(tenant_id="tenant-1", user_id="user-1"),
    )
    assert response.catalog_digest == first.catalog_digest


def test_tool_catalog_runtime_search_failure_does_not_publish_snapshot():
    runtime = ToolCatalogRuntime(
        registry_factory=ToolRegistry,
        search_service_factory=lambda registry: (_ for _ in ()).throw(
            RuntimeError("embedding unavailable")
        ),
    )

    with pytest.raises(RuntimeError, match="embedding unavailable"):
        runtime.open()

    with pytest.raises(RuntimeError, match="tool_catalog_unavailable"):
        runtime.check_health()
    with pytest.raises(RuntimeError, match="tool_search_unavailable"):
        _ = runtime.search_service


def test_tool_catalog_runtime_close_discards_registry_and_search_service():
    builds = []
    runtime = ToolCatalogRuntime(
        registry_factory=ToolRegistry,
        search_service_factory=lambda registry: (
            builds.append(registry.catalog_digest) or _SearchService(registry.catalog_digest)
        ),
    )

    first = runtime.open()
    runtime.close()
    second = runtime.open()

    assert first is not second
    assert len(builds) == 2


def test_tool_catalog_runtime_refreshes_after_ttl():
    now = [0.0]
    builds = []

    def discover():
        builds.append(len(builds) + 1)
        return ToolRegistry(build_revision=f"build-{builds[-1]}")

    runtime = ToolCatalogRuntime(
        registry_factory=discover,
        refresh_seconds=30,
        clock=lambda: now[0],
    )

    first = runtime.open()
    now[0] = 29
    assert runtime.open() is first
    now[0] = 31
    second = runtime.open()

    assert second is not first
    assert builds == [1, 2]


def test_tool_catalog_runtime_keeps_last_snapshot_when_refresh_fails():
    now = [0.0]
    calls = [0]

    def discover():
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("admin unavailable")
        return ToolRegistry()

    runtime = ToolCatalogRuntime(
        registry_factory=discover,
        refresh_seconds=30,
        clock=lambda: now[0],
    )
    first = runtime.open()
    now[0] = 31

    assert runtime.open() is first
    runtime.check_health()
    assert calls == [2]


def test_tool_catalog_runtime_throttles_refresh_after_failure():
    now = [0.0]
    calls = [0]

    def discover():
        calls[0] += 1
        if calls[0] > 1:
            raise RuntimeError("admin unavailable")
        return ToolRegistry()

    runtime = ToolCatalogRuntime(
        registry_factory=discover,
        refresh_seconds=30,
        clock=lambda: now[0],
    )
    first = runtime.open()
    now[0] = 31
    assert runtime.open() is first
    now[0] = 32
    assert runtime.open() is first

    assert calls == [2]
