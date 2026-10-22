from threading import Barrier, Event, Thread
from typing import ClassVar

import pytest
from app.conversations.service import BeginTurn, ConversationService
from app.storage.models import AgentMessage, AgentRun, AgentSession, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'turns.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def prepare(database):
    from app.conversations.durable import DurableRunService

    with database() as db:
        handle = ConversationService(db).begin_turn(
            BeginTurn(
                tenant_id="t", user_id="u", session_id="s", request_id="r", user_content="问题"
            )
        )
        DurableRunService(db).prepare(handle=handle, payload={})
        db.commit()
    return handle


@pytest.mark.parametrize(
    ("server", "remote_name", "result_status", "receipt_status"),
    [
        ("tushare", "income_vip", "denied", "settled"),
        ("tushare", "write_account", "failed", "unknown"),
        ("other", "income_vip", "failed", "unknown"),
    ],
)
def test_mcp_permission_denial_only_settles_verified_read_only_receipts(
    database, monkeypatch, server, remote_name, result_status, receipt_status
):
    import asyncio

    from app.conversations.durable import DurableRunService
    from app.conversations.execution import (
        ExecutionControl,
        UnresolvedToolEffect,
        execution_control,
    )
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_mcp_http_tool import _langchain_tool
    from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
    from app.tools.mcp_http import DynamicMcpHttpTool
    from app.tools.registry import ExecutionBinding, ToolRegistry
    from langchain_core.messages import ToolMessage
    from sqlalchemy import select

    calls = []

    async def rejected(_value):
        calls.append(True)
        return ToolMessage(content="抱歉，您没有访问该接口的权限", tool_call_id="remote", status="error")

    tool = DynamicMcpHttpTool(
        name=f"mcp.{server}.{remote_name}", server_name=server,
        langchain_tool=_langchain_tool(remote_name, {}), description="验证权限边界",
        input_schema={"type": "object", "properties": {}}, risk_level="medium",
    )
    monkeypatch.setattr(type(tool._langchain_tool), "ainvoke", lambda self, value: rejected(value))
    registry = ToolRegistry()
    registry.register(tool, execution_binding=ExecutionBinding(
        provider="mcp", implementation_id=f"mcp.http.{tool.name}", server_id=server,
        deployment_revision="test", config_revision="test",
    ))
    handle = prepare(database)
    with database() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    control = ExecutionControl(database, handle.run_id, owner)
    with execution_control(control):
        result = asyncio.run(ToolExecutionGateway(registry).execute(
            ToolCallRequest(tool_name=tool.name, call_id="permission-test", arguments={}, purpose="验证"),
            trace_id=handle.trace_id, context=ToolExecutionContext(
                tenant_id="t", user_id="u", session_id="s", allowed_tools=(tool.name,),
                permission_scopes=(f"mcp:{server}",),
            ),
        ))
    assert result.status == result_status
    assert result.error.code == "provider_permission_denied"
    assert len(calls) == 1
    with database() as db:
        receipt = db.scalar(select(AgentRunToolReceipt))
        assert receipt.status == receipt_status
        assert receipt.result_summary["status"] == result_status
    if receipt_status == "settled":
        control.ensure_tool_effects_resolved(risk_level="medium")
        control.ensure_tool_effects_resolved(risk_level="high")
    else:
        with pytest.raises(UnresolvedToolEffect):
            control.ensure_tool_effects_resolved(risk_level="high")


def test_persisted_events_replay_in_order_and_keep_partial_answer(database):
    from app.conversations.durable import DurableRunService

    handle = prepare(database)
    with database() as db:
        service = DurableRunService(db)
        owner = service.claim(handle.run_id)
        assert owner
        service.append(handle.run_id, owner, "messages", [{"content": "部分正文"}, {}])
        db.commit()
    with database() as db:
        service = DurableRunService(db)
        events = service.events(handle.run_id, after=0)
        assert [event.sequence for event in events] == [1, 2]
        assert events[0].data["type"] == "run_started"
        assert service.events(handle.run_id, after=1)[0].sequence == 2
        assert db.get(AgentMessage, handle.assistant_message_id).content == "部分正文"


def test_stop_is_cooperative_idempotent_and_does_not_overwrite_completed(database):
    from app.conversations.durable import DurableRunService

    handle = prepare(database)
    with database() as db:
        service = DurableRunService(db)
        owner = service.claim(handle.run_id)
        service.append(handle.run_id, owner, "messages", [{"content": "已生成"}, {}])
        assert service.request_stop(handle.run_id) == "stop_requested"
        assert service.request_stop(handle.run_id) == "stop_requested"
        assert db.get(AgentSession, handle.session_key).active_run_id == handle.run_id
        service.finish_cancelled(handle.run_id, owner)
        db.commit()
    with database() as db:
        assert db.get(AgentRun, handle.run_id).status == "cancelled"
        assert db.get(AgentMessage, handle.assistant_message_id).content == "已生成"
        assert db.get(AgentMessage, handle.assistant_message_id).status == "cancelled"
        assert db.get(AgentSession, handle.session_key).active_run_id is None
        service = ConversationService(db)
        next_handle = service.begin_turn(
            BeginTurn(
                tenant_id="t", user_id="u", session_id="s", request_id="r2", user_content="继续"
            )
        )
        service.complete_turn(tenant_id="t", run_id=next_handle.run_id, content="完成")
        assert DurableRunService(db).request_stop(next_handle.run_id) == "completed"
        db.commit()


def test_owner_fencing_rejects_late_event_and_duplicate_producer(database):
    from app.conversations.durable import DurableRunService, RunOwnershipLost

    handle = prepare(database)
    with database() as db:
        service = DurableRunService(db)
        owner = service.claim(handle.run_id)
        assert service.claim(handle.run_id) is None
        db.commit()
        with pytest.raises(RunOwnershipLost):
            service.append(handle.run_id, "old-owner", "custom", {"type": "progress"})
        service.request_stop(handle.run_id)
        service.finish_cancelled(handle.run_id, owner)
        db.commit()
    with database() as db:
        with pytest.raises(RunOwnershipLost):
            DurableRunService(db).append(
                handle.run_id, owner, "messages", [{"content": "迟到"}, {}]
            )
        assert db.get(AgentMessage, handle.assistant_message_id).content == ""


def test_subscription_disconnect_does_not_close_background_producer(database):
    from app.conversations.durable import DurableRunService
    from app.conversations.execution import produce_frames, subscribe_events

    handle = prepare(database)
    entered, proceed = Event(), Event()

    def frames(_db):
        yield 'event: messages\ndata: [{"content":"第一段"},{}]\n\n'
        entered.set()
        assert proceed.wait(3)
        yield 'event: messages\ndata: [{"content":"第二段"},{}]\n\n'
        with database() as db:
            ConversationService(db).complete_turn(
                tenant_id="t", run_id=handle.run_id, content="第一段第二段"
            )
            db.commit()

    producer = Thread(
        target=produce_frames,
        kwargs={"session_factory": database, "run_id": handle.run_id, "frames_factory": frames},
    )
    producer.start()
    assert entered.wait(3)
    subscriber = subscribe_events(session_factory=database, run_id=handle.run_id, after=0)
    assert "run_started" in next(subscriber)
    subscriber.close()
    proceed.set()
    producer.join(3)
    assert not producer.is_alive()
    with database() as db:
        events = DurableRunService(db).events(handle.run_id, after=0)
        assert [event.sequence for event in events] == [1, 2, 3, 4]
        assert events[-1].data == {
            "type": "run_status",
            "run_id": handle.run_id,
            "status": "completed",
        }
        assert db.get(AgentMessage, handle.assistant_message_id).content == "第一段第二段"


def test_unknown_write_result_does_not_block_read_turn_but_blocks_new_writes(database):
    from app.conversations.durable import DurableRunService
    from app.conversations.execution import ExecutionControl, UnresolvedToolEffect
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt

    handle = prepare(database)
    with database() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    control = ExecutionControl(database, handle.run_id, owner)
    request = ToolCallRequest(
        tool_name="write_account", arguments={}, purpose="写入", call_id="write-1"
    )
    receipt_id = control.tool_started(request, risk_level="high")
    control.tool_finished(
        receipt_id, {"status": "timeout", "error": {"code": "provider_timeout"}}, uncertain=True
    )
    with database() as db:
        service = DurableRunService(db)
        service.request_stop(handle.run_id)
        service.finish_cancelled(handle.run_id, owner)
        db.commit()
        assert db.get(AgentRunToolReceipt, receipt_id).status == "unknown"
        assert db.get(AgentSession, handle.session_key).active_run_id is None
        next_handle = ConversationService(db).begin_turn(
            BeginTurn(
                tenant_id="t", user_id="u", session_id="s", request_id="r3", user_content="查询"
            )
        )
        service.prepare(handle=next_handle, payload={})
        next_owner = service.claim(next_handle.run_id)
        db.commit()
    next_control = ExecutionControl(database, next_handle.run_id, next_owner)
    next_control.ensure_tool_effects_resolved(risk_level="low")
    with pytest.raises(UnresolvedToolEffect):
        next_control.ensure_tool_effects_resolved(risk_level="high")


def test_running_tool_cannot_be_cancelled_before_result_is_saved(database):
    from app.conversations.durable import DurableRunService
    from app.conversations.execution import ExecutionControl
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt

    handle = prepare(database)
    with database() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    control = ExecutionControl(database, handle.run_id, owner)
    receipt_id = control.tool_started(
        ToolCallRequest(tool_name="query", arguments={}, purpose="查询"), risk_level="low"
    )
    with database() as db:
        service = DurableRunService(db)
        service.request_stop(handle.run_id)
        service.finish_cancelled(handle.run_id, owner)
        db.commit()
        assert db.get(AgentSession, handle.session_key).active_run_id == handle.run_id
    control.tool_finished(
        receipt_id, {"status": "success", "result": {"api_key": "secret-value", "value": 42}}
    )
    with database() as db:
        DurableRunService(db).finish_cancelled(handle.run_id, owner)
        db.commit()
        receipt = db.get(AgentRunToolReceipt, receipt_id)
        assert receipt.result_summary["result"] == {"api_key": "***", "value": 42}
        assert receipt.status == "settled"
        assert db.get(AgentSession, handle.session_key).active_run_id is None


def test_restart_does_not_fail_live_durable_owner_even_with_stale_heartbeat(database):
    from app.conversations.durable import DurableRunService
    from app.conversations.recovery import recover_interrupted_local_runs

    handle = prepare(database)
    with database() as db:
        DurableRunService(db).claim(handle.run_id)
        db.commit()
    assert recover_interrupted_local_runs(database.kw["bind"]) == 0
    with database() as db:
        assert db.get(AgentRun, handle.run_id).status == "running"


def test_confirmed_dead_owner_is_fenced_and_unknown_write_does_not_keep_session_busy(
    database, monkeypatch
):
    from app.conversations import recovery
    from app.conversations.durable import DurableRunService, RunOwnershipLost
    from app.conversations.execution import ExecutionControl
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt

    handle = prepare(database)
    with database() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    receipt_id = ExecutionControl(database, handle.run_id, owner).tool_started(
        ToolCallRequest(tool_name="write", arguments={}, purpose="写入"), risk_level="high"
    )
    with database() as db:
        DurableRunService(db).request_stop(handle.run_id)
        db.commit()
    monkeypatch.setattr(recovery, "_local_owner_is_dead", lambda _: True)
    with database() as db:
        from app.storage.models import AgentRunExecution

        db.get(AgentRunExecution, handle.run_id).owner_pid = 987654
        db.commit()
    assert recovery.recover_orphaned_durable_runs(database.kw["bind"]) == 1
    assert recovery.recover_orphaned_durable_runs(database.kw["bind"]) == 0
    with database() as db:
        assert db.get(AgentRunToolReceipt, receipt_id).status == "unknown"
        assert db.get(AgentRun, handle.run_id).status == "cancelled"
        assert db.get(AgentSession, handle.session_key).active_run_id is None
        with pytest.raises(RunOwnershipLost):
            DurableRunService(db).append(
                handle.run_id, owner, "messages", [{"content": "迟到"}, {}]
            )


def test_node_cancellation_guard_prevents_calling_next_node(database):
    from app.conversations.durable import DurableRunService, TurnCancelled
    from app.conversations.execution import ExecutionControl, execution_control
    from app.harness.node_runner import NodeRunner

    handle = prepare(database)
    with database() as db:
        service = DurableRunService(db)
        owner = service.claim(handle.run_id)
        service.request_stop(handle.run_id)
        db.commit()
    calls = []
    with (
        execution_control(ExecutionControl(database, handle.run_id, owner)),
        pytest.raises(TurnCancelled),
    ):
        NodeRunner().run("next", lambda state: calls.append(state), {})
    assert calls == []


def test_gateway_keeps_executing_tool_result_before_confirming_cancel(database):
    import asyncio

    from app.conversations.durable import DurableRunService, RunOwnershipLost
    from app.conversations.execution import ExecutionControl, execution_control
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_tool_agent import EchoTool, _context
    from app.tools.execution_gateway import ToolExecutionGateway
    from app.tools.registry import ToolRegistry
    from sqlalchemy import select

    started, release = Event(), Event()
    errors = []

    class WaitingTool(EchoTool):
        async def run(self, data):
            started.set()
            assert release.wait(5)
            return await super().run(data)

    handle = prepare(database)
    with database() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    registry = ToolRegistry()
    registry.register(WaitingTool())

    def execute():
        try:
            with execution_control(ExecutionControl(database, handle.run_id, owner)):
                asyncio.run(
                    ToolExecutionGateway(registry).execute(
                        ToolCallRequest(
                            tool_name="echo", arguments={"text": "工具结果"}, purpose="验证"
                        ),
                        trace_id=handle.trace_id,
                        context=_context(),
                    )
                )
        except BaseException as exc:  # noqa: BLE001 - 取消控制信号刻意继承 BaseException
            errors.append(exc)

    worker = Thread(target=execute)
    worker.start()
    assert started.wait(5)
    try:
        with database() as db:
            assert db.scalar(select(AgentRunToolReceipt)).status == "running"
            assert DurableRunService(db).request_stop(handle.run_id) == "stop_requested"
            db.commit()
            assert db.get(AgentSession, handle.session_key).active_run_id == handle.run_id
    finally:
        release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RunOwnershipLost)
    with database() as db:
        receipt = db.scalar(select(AgentRunToolReceipt))
        assert receipt.status == "settled"
        assert receipt.result_summary["result"]["text"] == "工具结果"
        assert db.get(AgentRun, handle.run_id).status == "cancelled"
        assert db.get(AgentSession, handle.session_key).active_run_id is None


def test_stop_interrupted_turn_is_cancelled_without_resuming_graph(database):
    from app.conversations.durable import DurableRunService

    handle = prepare(database)
    with database() as db:
        durable = DurableRunService(db)
        owner = durable.claim(handle.run_id)
        ConversationService(db).interrupt_turn(tenant_id="t", run_id=handle.run_id)
        durable.publish_status(handle.run_id, owner)
        db.commit()
    with database() as db:
        assert DurableRunService(db).request_stop(handle.run_id) == "cancelled"
        db.commit()
        assert db.get(AgentSession, handle.session_key).active_run_id is None


def test_finished_producer_evidence_recovers_thread_failure_without_killing_live_process(
    database, monkeypatch
):
    from app.conversations import execution, recovery
    from sqlalchemy.exc import SQLAlchemyError

    handle = prepare(database)

    def frames(_db):
        raise RuntimeError("producer failed")
        yield

    def persistence_unavailable(*_args, **_kwargs):
        raise SQLAlchemyError("database temporarily unavailable")

    monkeypatch.setattr(execution, "_finish_producer", persistence_unavailable)
    with pytest.raises(SQLAlchemyError):
        execution.produce_frames(
            session_factory=database, run_id=handle.run_id, frames_factory=frames
        )
    assert recovery.recover_orphaned_durable_runs(database.kw["bind"]) == 1
    with database() as db:
        assert db.get(AgentRun, handle.run_id).status == "failed"
        assert db.get(AgentSession, handle.session_key).active_run_id is None


def test_outbox_redelivery_only_requeues_unclaimed_turns(database):
    from app.conversations.durable import DurableRunService
    from app.conversations.recovery import dispatch_unclaimed_runs
    from app.storage.models import AgentRunExecution

    handle = prepare(database)
    with database() as db:
        db.get(AgentRunExecution, handle.run_id).payload = {"handle": {"run_id": handle.run_id}}
        db.commit()
    calls = []
    assert (
        dispatch_unclaimed_runs(database.kw["bind"], dispatch=calls.append, older_than_seconds=0)
        == 1
    )
    assert calls == [handle.run_id]
    with database() as db:
        DurableRunService(db).claim(handle.run_id)
        db.commit()
    assert (
        dispatch_unclaimed_runs(database.kw["bind"], dispatch=calls.append, older_than_seconds=0)
        == 0
    )


def test_idle_subscription_yields_heartbeat_and_remains_closeable(database, monkeypatch):
    from app.conversations import execution

    handle = prepare(database)
    ticks = iter(range(100))
    monkeypatch.setattr(execution, "monotonic", lambda: next(ticks), raising=False)
    sleeps = []

    def idle_wait(_):
        sleeps.append(True)
        assert len(sleeps) < 20, "空闲订阅必须让出同步 next，允许 ASGI 接收断线取消"

    monkeypatch.setattr(execution, "sleep", idle_wait)
    stream = execution.subscribe_events(session_factory=database, run_id=handle.run_id)
    assert "run_started" in next(stream)
    assert next(stream) == ": heartbeat\n\n"
    stream.close()
    with database() as db:
        assert db.get(AgentRun, handle.run_id).status == "running"


def test_invalid_write_output_is_unknown_and_cannot_be_blindly_retried(database):
    import asyncio

    from app.conversations.durable import DurableRunService
    from app.conversations.execution import ExecutionControl, execution_control
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_tool_agent import EchoTool, _context
    from app.tools.execution_gateway import ToolExecutionGateway
    from app.tools.registry import ToolRegistry
    from sqlalchemy import select

    calls = []

    class InvalidWriteTool(EchoTool):
        risk_level = "medium"
        output_schema: ClassVar[dict] = {"type": "object", "required": ["confirmed"]}

        async def run(self, data):
            calls.append(data.text)
            return await super().run(data)

    handle = prepare(database)
    with database() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    registry = ToolRegistry()
    registry.register(InvalidWriteTool())
    gateway = ToolExecutionGateway(registry)
    request = ToolCallRequest(tool_name="echo", arguments={"text": "write"}, purpose="写入")
    with execution_control(ExecutionControl(database, handle.run_id, owner)):
        first = asyncio.run(gateway.execute(request, trace_id=handle.trace_id, context=_context()))
        second = asyncio.run(gateway.execute(request, trace_id=handle.trace_id, context=_context()))
    assert first.error.code == "tool_output_validation_failed"
    assert second.error.code == "tool_side_effects_unresolved"
    assert calls == ["write"]
    with database() as db:
        assert db.scalar(select(AgentRunToolReceipt)).status == "unknown"


def test_aborted_tool_coroutine_keeps_unknown_receipt_instead_of_permanent_running(database):
    import asyncio

    from app.conversations.durable import DurableRunService
    from app.conversations.execution import ExecutionControl, execution_control
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_tool_agent import EchoTool, _context
    from app.tools.execution_gateway import ToolExecutionGateway
    from app.tools.registry import ToolRegistry
    from sqlalchemy import select

    class AbortedTool(EchoTool):
        risk_level = "medium"

        async def run(self, _):
            raise asyncio.CancelledError()

    handle = prepare(database)
    with database() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    registry = ToolRegistry()
    registry.register(AbortedTool())
    with (
        execution_control(ExecutionControl(database, handle.run_id, owner)),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(
            ToolExecutionGateway(registry).execute(
                ToolCallRequest(tool_name="echo", arguments={"text": "write"}, purpose="写入"),
                trace_id=handle.trace_id,
                context=_context(),
            )
        )
    with database() as db:
        assert db.scalar(select(AgentRunToolReceipt)).status == "unknown"


def test_returned_tool_result_retries_persistence_after_database_recovers(database):
    from app.conversations.durable import DurableRunService
    from app.conversations.execution import ExecutionControl, retry_pending_tool_results
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt
    from sqlalchemy.exc import SQLAlchemyError

    handle = prepare(database)
    with database() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    control = ExecutionControl(database, handle.run_id, owner)
    receipt_id = control.tool_started(
        ToolCallRequest(tool_name="write", arguments={}, purpose="写入"), risk_level="medium"
    )

    class TemporarilyUnavailable:
        kw = database.kw
        available = False

        def __call__(self):
            if not self.available:
                raise SQLAlchemyError("database unavailable")
            return database()

    unavailable = TemporarilyUnavailable()
    interrupted_control = ExecutionControl(unavailable, handle.run_id, owner)
    with pytest.raises(SQLAlchemyError):
        interrupted_control.tool_finished(
            receipt_id, {"status": "success", "result": {"confirmed": True}}
        )
    unavailable.available = True
    assert retry_pending_tool_results(database.kw["bind"]) == 1
    with database() as db:
        receipt = db.get(AgentRunToolReceipt, receipt_id)
        assert receipt.status == "settled"
        assert receipt.result_summary["result"] == {"confirmed": True}


def test_producer_cannot_drop_returned_result_when_first_persistence_fails(database):
    from app.conversations.execution import (
        ExecutionControl,
        current_execution,
        produce_frames,
        retry_pending_tool_results,
    )
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    handle = prepare(database)

    class FailsOnce:
        kw = database.kw
        failed = False

        def __call__(self):
            if not self.failed:
                self.failed = True
                raise SQLAlchemyError("transient database error")
            return database()

    def frames(_db):
        control = current_execution.get()
        receipt_id = control.tool_started(
            ToolCallRequest(tool_name="write", arguments={}, purpose="写入"), risk_level="medium"
        )
        ExecutionControl(FailsOnce(), control.run_id, control.owner).tool_finished(
            receipt_id, {"status": "success", "result": {"confirmed": True}}
        )
        yield

    produce_frames(session_factory=database, run_id=handle.run_id, frames_factory=frames)
    with database() as db:
        receipt = db.scalar(select(AgentRunToolReceipt))
        active = db.get(AgentSession, handle.session_key).active_run_id
        assert receipt.status != "running" or active == handle.run_id
    retry_pending_tool_results(database.kw["bind"])
    with database() as db:
        receipt = db.scalar(select(AgentRunToolReceipt))
        assert receipt.status == "settled"
        assert receipt.result_summary["result"] == {"confirmed": True}


def test_confirmed_write_call_id_is_not_executed_twice_on_approval_retry(database):
    import asyncio

    from app.conversations.durable import DurableRunService
    from app.conversations.execution import ExecutionControl, execution_control
    from app.schemas.tools import ToolCallRequest
    from app.tests.unit.test_tool_agent import EchoTool, _context
    from app.tools.execution_gateway import ToolExecutionGateway
    from app.tools.registry import ToolRegistry

    calls = []

    class WriteTool(EchoTool):
        risk_level = "medium"

        async def run(self, data):
            calls.append(data.text)
            return await super().run(data)

    handle = prepare(database)
    with database() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    registry = ToolRegistry()
    registry.register(WriteTool())
    gateway = ToolExecutionGateway(registry)
    request = ToolCallRequest(
        tool_name="echo", call_id="trusted-call-1", arguments={"text": "write"}, purpose="写入"
    )
    with execution_control(ExecutionControl(database, handle.run_id, owner)):
        assert (
            asyncio.run(
                gateway.execute(request, trace_id=handle.trace_id, context=_context())
            ).status
            == "success"
        )
        replay = asyncio.run(gateway.execute(request, trace_id=handle.trace_id, context=_context()))
    assert replay.status == "denied"
    assert replay.error.code == "tool_call_already_executed"
    assert calls == ["write"]


def test_parallel_write_dispatch_keeps_single_unsettled_call(database):
    from app.conversations.durable import DurableRunService
    from app.conversations.execution import ExecutionControl, UnresolvedToolEffect
    from app.schemas.tools import ToolCallRequest

    handle = prepare(database)
    with database() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    control = ExecutionControl(database, handle.run_id, owner)
    ready = Barrier(2)
    receipts, failures = [], []

    def dispatch(call_id):
        ready.wait(5)
        try:
            receipts.append(
                control.tool_started(
                    ToolCallRequest(
                        tool_name="write", call_id=call_id, arguments={}, purpose="写入"
                    ),
                    risk_level="medium",
                )
            )
        except UnresolvedToolEffect as exc:
            failures.append(exc)

    workers = [Thread(target=dispatch, args=(f"call-{i}",)) for i in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    assert len(receipts) == len(failures) == 1


def test_legacy_stream_failure_cannot_release_turn_with_unsettled_tool_result(database):
    from app.conversations.durable import DurableRunService, TurnCancelled
    from app.conversations.execution import ExecutionControl, execution_control
    from app.schemas.tools import ToolCallRequest

    handle = prepare(database)
    with database() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    control = ExecutionControl(database, handle.run_id, owner)
    control.tool_started(
        ToolCallRequest(tool_name="write", arguments={}, purpose="写入"), risk_level="medium"
    )
    with execution_control(control), database() as db, pytest.raises(TurnCancelled):
        ConversationService(db).fail_turn(
            tenant_id="t", run_id=handle.run_id, error_code="agent_execution_failed"
        )
    with database() as db:
        assert db.get(AgentSession, handle.session_key).active_run_id == handle.run_id
