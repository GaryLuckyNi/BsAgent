from app.tools.catalog_runtime import ToolCatalogRuntime
from app.tools.registry import ToolRegistry


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
