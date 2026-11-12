"""派发前数据库等待的取消不得遗留running回执。"""
import asyncio
import threading

import pytest
from app.conversations.execution import execution_control
from app.schemas.tools import ToolCallRequest
from app.storage.models import AgentRunToolReceipt
from app.tests.unit import test_run_authorization
from app.tests.unit.test_run_authorization import binding_context, control_for, initialize
from app.tests.unit.test_tool_agent import EchoTool
from app.tools.execution_gateway import ToolExecutionGateway
from app.tools.registry import ToolRegistry
from sqlalchemy import select

environment = test_run_authorization.environment


@pytest.mark.parametrize("phase", ["refresh", "charge"])
def test_cancel_before_dispatch_settles_not_started_receipt(environment, monkeypatch, phase):
    import app.tools.execution_gateway as gateway
    initialize(environment)
    control = control_for(environment)
    registry, calls = ToolRegistry(), []
    class Counting(EchoTool):
        async def run(self, data):
            calls.append(data.text)
            return await super().run(data)
    registry.register(Counting())
    entered, release = threading.Event(), threading.Event()
    original, seen = gateway.refresh_tool_context, []
    def refresh(context):
        seen.append(1)
        if phase == "refresh" and len(seen) == 2:
            entered.set()
            assert release.wait(5)
        return original(context)
    original_charge = gateway.charge
    def charge(*args, **kwargs):
        if phase == "charge":
            entered.set()
            assert release.wait(5)
        return original_charge(*args, **kwargs)
    monkeypatch.setattr(gateway, "refresh_tool_context", refresh)
    monkeypatch.setattr(gateway, "charge", charge)
    async def run():
        with execution_control(control):
            task = asyncio.create_task(ToolExecutionGateway(registry).execute(
                ToolCallRequest(tool_name="echo", arguments={"text": "公开数据"}, purpose="测试"),
                trace_id=environment[4].trace_id, context=binding_context(environment)))
            try:
                async with asyncio.timeout(5):
                    while not entered.is_set():
                        await asyncio.sleep(.001)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
    asyncio.run(run())
    assert calls == []
    with environment[0]() as db:
        receipt = db.scalar(select(AgentRunToolReceipt))
        assert receipt.status == "settled"
        assert receipt.result_summary["execution_state"] == "not_started"


def test_revoke_during_budget_wait_is_checked_before_dispatch(environment, monkeypatch):
    import app.tools.execution_gateway as gateway
    initialize(environment)
    control = control_for(environment)
    registry, calls = ToolRegistry(), []
    class Counting(EchoTool):
        async def run(self, data):
            calls.append(data.text)
            return await super().run(data)
    registry.register(Counting())
    entered, release = threading.Event(), threading.Event()
    original = gateway.charge
    def charge(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(gateway, "charge", charge)
    async def run():
        with execution_control(control):
            task = asyncio.create_task(ToolExecutionGateway(registry).execute(
                ToolCallRequest(tool_name="echo", arguments={"text": "公开数据"}, purpose="测试"),
                trace_id=environment[4].trace_id, context=binding_context(environment)))
            try:
                async with asyncio.timeout(5):
                    while not entered.is_set():
                        await asyncio.sleep(.001)
                environment[1].revoke_membership(tenant_id="t", user_id=environment[3].user_id, actor_ref="test")
            finally:
                release.set()
            result = await task
            assert result.status == "denied"
    asyncio.run(run())
    assert calls == []
