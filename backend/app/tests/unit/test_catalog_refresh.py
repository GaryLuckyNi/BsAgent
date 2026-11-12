"""一个 run 内目录固定，变化只在调用前变成模型可见工具错误。"""

import asyncio

import pytest
from app.schemas.tools import ToolCallRequest, ToolCallResult
from app.tests.unit.test_durable_turns import database, prepare  # noqa: F401
from app.tools.base import Tool
from app.tools.catalog_runtime import ToolCatalogRuntime
from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
from app.tools.registry import ExecutionBinding, ToolRegistry
from pydantic import BaseModel


class EmptyInput(BaseModel):
    pass


class ReadTool(Tool[EmptyInput]):
    name = "read_facts"
    version = "1"
    input_model = EmptyInput

    def __init__(self, calls, on_run=lambda: None):
        self.calls = calls
        self.on_run = on_run

    async def run(self, input_data):
        self.calls.append(self.name)
        self.on_run()
        return ToolCallResult(tool_call_id="remote", tool_name=self.name, status="success",
                              result={"fact": 1}, trace_id="")


def registry_for(tool, revision="1"):
    registry = ToolRegistry()
    registry.register(tool, execution_binding=ExecutionBinding(
        provider="local", implementation_id="read", deployment_revision=revision,
    ))
    return registry.snapshot()


def invoke(captured, latest):
    return asyncio.run(ToolExecutionGateway(
        captured, availability_registry_factory=lambda: latest[0],
    ).execute(
        ToolCallRequest(tool_name="read_facts", tool_version="1", arguments={}, purpose="读取"),
        trace_id="trace", context=ToolExecutionContext(
            tenant_id="t", user_id="u", session_id="s", allowed_tools=("read_facts",),
        ),
    ))


def test_run_keeps_pair_across_refresh_and_new_run_uses_new_pair():
    current = [registry_for(ReadTool([]))]
    runtime = ToolCatalogRuntime(registry_factory=lambda: current[0])
    first = runtime.snapshot_pair_for_run(run_key="run-a")
    current[0] = registry_for(ReadTool([]), revision="2")
    runtime.request_refresh()
    runtime._refresh_thread.join(timeout=5)
    assert not runtime._refresh_thread.is_alive()
    assert runtime.snapshot_pair_for_run(run_key="run-a") == first
    second = runtime.snapshot_pair_for_run(run_key="run-b")
    assert second[0].catalog_digest != first[0].catalog_digest
    runtime.release_run_snapshot("run-a")
    assert runtime.get_run_snapshot("run-a") is None
    runtime.close()


@pytest.mark.parametrize("change,code", [("removed", "tool_not_found"), ("contract", "tool_catalog_changed"),
                                        ("binding", "tool_catalog_changed")])
def test_changed_tool_is_not_dispatched(change, code):
    calls = []
    captured = registry_for(ReadTool(calls))
    if change == "removed":
        latest = ToolRegistry().snapshot()
    else:
        changed = ReadTool(calls)
        if change == "contract":
            changed.description = "新的契约说明"
        latest = registry_for(changed, revision="2" if change == "binding" else "1")
    result = invoke(captured, [latest])
    assert result.status == "failed" and result.execution_state == "not_started"
    assert result.error.code == code
    assert calls == []


def test_unrelated_catalog_change_does_not_invalidate_captured_tool():
    calls = []
    captured = registry_for(ReadTool(calls))
    latest = captured.mutable_copy()
    other = ReadTool(calls)
    other.name = "other_tool"
    latest.register(other)
    result = invoke(captured, [latest.snapshot()])
    assert result.status == "success" and calls == ["read_facts"]


def test_documentation_refresh_does_not_invalidate_captured_tool():
    calls = []
    captured = registry_for(ReadTool(calls))
    updated = ReadTool(calls)
    updated.documentation = {"status": "partial", "output_fields": {"fact": {"description": "事实值"}}}
    latest = registry_for(updated)
    assert latest.catalog_digest == captured.catalog_digest

    result = invoke(captured, [latest])

    assert result.status == "success" and calls == ["read_facts"]
    assert captured.get("read_facts").spec.documentation == {}


def test_refresh_after_dispatch_does_not_discard_or_repeat_success():
    calls, latest = [], []
    tool = ReadTool(calls, on_run=lambda: latest.__setitem__(0, ToolRegistry().snapshot()))
    captured = registry_for(tool)
    latest.append(captured)
    result = invoke(captured, latest)
    assert result.status == "success" and result.execution_state == "completed"
    assert result.result == {"fact": 1} and calls == ["read_facts"]


def test_failed_producer_releases_run_snapshot(database, monkeypatch):  # noqa: F811 - pytest 引入的 fixture
    from app.conversations.execution import produce_frames
    from app.graph.state import build_checkpoint_thread_id
    from app.storage.models import AgentRun
    from app.tools import default_registry

    handle = prepare(database)
    catalog = ToolCatalogRuntime(registry_factory=ToolRegistry)
    monkeypatch.setattr(default_registry, "get_default_tool_catalog_runtime", lambda: catalog)
    key = build_checkpoint_thread_id(tenant_id="t", session_key=handle.session_key, request_id=handle.request_id)
    catalog.snapshot_pair_for_run(run_key=key)

    def fail(db):
        raise RuntimeError("执行异常")
        yield

    produce_frames(session_factory=database, run_id=handle.run_id, frames_factory=fail)
    with database() as db:
        assert db.get(AgentRun, handle.run_id).status == "failed"
    assert catalog.get_run_snapshot(key) is None
    catalog.close()


def test_valid_interrupt_retains_snapshot_until_stop_commits(monkeypatch):
    from app.graph.state import build_checkpoint_thread_id
    from app.tests.integration.test_durable_run_api import _pending_input_turn
    from app.tests.integration.test_session_api import create_test_app_with_database
    from app.tools import default_registry
    from fastapi.testclient import TestClient

    app, session_factory = create_test_app_with_database()
    handle = _pending_input_turn(session_factory)
    catalog = ToolCatalogRuntime(registry_factory=ToolRegistry)
    monkeypatch.setattr(default_registry, "get_default_tool_catalog_runtime", lambda: catalog)
    key = build_checkpoint_thread_id(tenant_id="tenant_1", session_key=handle.session_key, request_id=handle.request_id)
    catalog.snapshot_pair_for_run(run_key=key)
    assert catalog.get_run_snapshot(key) is not None
    response = TestClient(app).post(f"/api/sessions/s1/runs/{handle.run_id}/stop", json={})
    assert response.json()["status"] == "cancelled"
    assert catalog.get_run_snapshot(key) is None
    catalog.close()


def test_graph_build_failure_does_not_leave_run_binding(monkeypatch):
    from app.graph import runtime

    catalog = ToolCatalogRuntime(registry_factory=ToolRegistry)

    def factory():
        return catalog.snapshot()

    monkeypatch.setattr(runtime, "get_default_tool_catalog_runtime", lambda: catalog)
    monkeypatch.setattr(runtime, "get_default_tool_registry", factory)
    monkeypatch.setattr(runtime, "_DEFAULT_TOOL_REGISTRY_FACTORY", factory)

    def fail(**kwargs):
        raise RuntimeError("建图失败")

    monkeypatch.setattr(runtime, "build_question_answer_graph", fail)
    with pytest.raises(RuntimeError, match="建图失败"):
        runtime.run_question_answer_graph(business_intent_catalog=object(), session_id="s",
            tenant_id="t", user_id="u", user_message="读取")
    assert catalog._run_snapshots == {}
    catalog.close()


@pytest.mark.parametrize("finish_path", ["orphan_recovery", "delayed_receipt"])
def test_delayed_terminal_commit_releases_run_snapshot(database, monkeypatch, finish_path):  # noqa: F811
    from app.conversations import execution, recovery
    from app.conversations.durable import DurableRunService
    from app.graph.state import build_checkpoint_thread_id
    from app.storage.models import AgentRun, AgentSession
    from app.tools import default_registry
    from sqlalchemy.exc import SQLAlchemyError

    handle = prepare(database)
    catalog = ToolCatalogRuntime(registry_factory=ToolRegistry)
    monkeypatch.setattr(default_registry, "get_default_tool_catalog_runtime", lambda: catalog)
    key = build_checkpoint_thread_id(tenant_id="t", session_key=handle.session_key, request_id=handle.request_id)
    catalog.snapshot_pair_for_run(run_key=key)
    if finish_path == "orphan_recovery":
        def frames(db):
            raise RuntimeError("执行失败")
            yield

        def fail_commit(*args, **kwargs):
            raise SQLAlchemyError("提交短暂失败")

        monkeypatch.setattr(execution, "_finish_producer", fail_commit)
        with pytest.raises(SQLAlchemyError):
            execution.produce_frames(session_factory=database, run_id=handle.run_id, frames_factory=frames)
        assert catalog.get_run_snapshot(key) is not None
        assert recovery.recover_orphaned_durable_runs(database.kw["bind"]) == 1
        expected = "failed"
    else:
        with database() as db:
            owner = DurableRunService(db).claim(handle.run_id)
            db.commit()
        control = execution.ExecutionControl(database, handle.run_id, owner)
        receipt_id = control.tool_started(
            ToolCallRequest(tool_name="write", arguments={}, purpose="写入"), risk_level="medium",
        )
        with database() as db:
            assert DurableRunService(db).request_stop(handle.run_id) == "stop_requested"
            db.commit()
        execution.note_producer_finished(control)
        assert catalog.get_run_snapshot(key) is not None
        control.tool_finished(receipt_id, {"status": "success", "result": {"confirmed": True}})
        expected = "cancelled"
    with database() as db:
        assert db.get(AgentRun, handle.run_id).status == expected
        assert db.get(AgentSession, handle.session_key).active_run_id is None
    assert catalog.get_run_snapshot(key) is None
    catalog.close()
