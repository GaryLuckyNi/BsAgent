from types import SimpleNamespace

import pytest
from app.tests.unit.test_source_attempt_queue import Source
from app.tests.unit.test_tool_documentation_metadata import meaning
from app.tests.unit.test_tool_documentation_repository import (
    repository as doc_repository,  # noqa: F401
)


def config(public=True):
    return SimpleNamespace(name="public", public_documentation=public, documentation_domains=["docs.example.com"],
                           config_revision="v1", deployment_revision="r1")


def test_private_mcp_does_not_read_cache_publish_or_search():
    from app.tool_documentation.service import DocumentationService

    service = DocumentationService(repository=None, publish=lambda key: pytest.fail("private server enqueued"))
    tools = [Source("mcp.internal.query", [])]
    service.prepare(tools, config(False))
    assert tools[0].spec.documentation == {}


def test_discovery_enqueues_once_and_future_snapshot_reads_ready_cache(request):
    store = request.getfixturevalue("doc_repository")
    from app.tool_documentation.service import DocumentationService

    queued = []
    service = DocumentationService(repository=store, publish=queued.append)
    tool = Source("mcp.public.query", [])
    service.prepare([tool], config())
    assert len(queued) == 1 and tool.spec.documentation["status"] == "missing"
    service.prepare([tool], config())
    assert len(queued) == 1
    job = store.claim(queued[0])
    document = {"status": "partial", "output_fields": {"income": meaning("营业收入")}}
    store.finish(queued[0], job["lease"], document)
    fresh = Source(tool.name, [])
    service.prepare([fresh], config())
    assert fresh.spec.documentation["output_fields"]["income"]["description"] == "营业收入"
    assert tool.spec.documentation["status"] == "missing"
    changed = Source(tool.name, [])
    different = config()
    different.config_revision = "v2"
    service.prepare([changed], different)
    assert len(queued) == 2 and changed.spec.documentation["status"] == "missing"


def test_publish_failure_never_breaks_directory_and_has_cooldown(request):
    store = request.getfixturevalue("doc_repository")
    from app.tool_documentation.service import DocumentationService

    calls = []
    def fail(key):
        calls.append(key)
        raise RuntimeError("broker failed")
    service = DocumentationService(repository=store, publish=fail)
    tool = Source("mcp.public.query", [])
    service.prepare([tool], config())
    service.prepare([tool], config())
    assert len(calls) == 1 and tool.spec.documentation["status"] == "missing"


def test_scan_has_bounded_prewarming(request):
    store = request.getfixturevalue("doc_repository")
    from app.tool_documentation.service import DocumentationService

    queued = []
    service = DocumentationService(repository=store, publish=queued.append, prewarm_limit=2)
    tools = [Source(f"mcp.public.query{i}", []) for i in range(5)]
    service.prepare(tools, config())
    assert len(queued) == 2
    service.prepare(tools, config())
    assert len(queued) == 4


def test_task_processes_only_once_and_keeps_unknown_semantics_nonblocking(request):
    from app.tool_documentation.service import DocumentationService

    store = request.getfixturevalue("doc_repository")
    service = DocumentationService(repository=store, publish=lambda key: None)
    key = "a" * 64
    store.reserve(key, {}, ["docs.example.com"])
    class Enricher:
        calls = 0
        async def enrich(self, **kwargs):
            self.calls += 1
            return {"status": "partial", "output_fields": {"income": meaning("营业收入")}}
    enricher = Enricher()
    service.process(key, enricher=enricher)
    service.process(key, enricher=enricher)
    assert enricher.calls == 1 and store.ready(key)["status"] == "partial"


def test_celery_task_has_isolated_queue_and_dispatches_persisted_key(monkeypatch):
    from app.tool_documentation import tasks
    from app.worker.celery_app import celery_app

    received = []
    monkeypatch.setattr(tasks, "get_documentation_service", lambda: SimpleNamespace(process=received.append))
    tasks.enrich_tool_documentation.run(key="a" * 64)
    assert received == ["a" * 64]
    assert celery_app.conf.task_routes[tasks.enrich_tool_documentation.name] == {"queue": "tool-documentation"}
    assert "app.tool_documentation.tasks" in celery_app.conf.include


def test_default_dispatch_does_not_wait_for_broker(monkeypatch):
    from threading import Event

    from app.tool_documentation import service

    entered, release = Event(), Event()
    def send(key):
        entered.set()
        release.wait(2)
    monkeypatch.setattr(service, "_send", send)
    try:
        future = service._publish("a" * 64)
        assert entered.wait(1)
        assert not future.done()
    finally:
        release.set()
        future.result(timeout=2)
