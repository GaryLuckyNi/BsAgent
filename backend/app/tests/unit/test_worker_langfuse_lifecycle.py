from importlib import import_module
from types import SimpleNamespace


def test_worker_configures_langfuse_and_drains_before_exporter_shutdown(monkeypatch):
    worker = import_module("app.worker.celery_app")
    graph = import_module("app.graph.runtime")
    calls = []
    runtime = SimpleNamespace(shutdown=lambda: calls.append("langfuse-shutdown"))
    finalizer = SimpleNamespace(close=lambda: calls.append("finalizer-drain"))
    monkeypatch.setattr(worker, "_worker_telemetry_runtime", None)
    monkeypatch.setattr(worker, "_worker_langfuse_runtime", None, raising=False)
    monkeypatch.setattr(worker, "_worker_langfuse_finalizer", None, raising=False)
    monkeypatch.setattr(worker, "configure_worker_telemetry", lambda **_: None)
    monkeypatch.setattr(
        worker, "configure_langfuse",
        lambda **_: calls.append("langfuse-init") or runtime, raising=False,
    )
    monkeypatch.setattr(
        worker, "build_langfuse_finalizer",
        lambda *_: calls.append("finalizer-init") or finalizer, raising=False,
    )
    monkeypatch.setattr(
        graph, "configure_langfuse_finalizer",
        lambda value: calls.append("bind" if value is finalizer else "unbind"),
    )
    worker.initialize_worker_telemetry()
    worker.shutdown_worker_telemetry()
    worker.shutdown_worker_telemetry()

    assert calls == [
        "langfuse-init", "finalizer-init", "bind", "finalizer-drain",
        "unbind", "langfuse-shutdown",
    ]
