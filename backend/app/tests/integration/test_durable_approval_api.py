from threading import Event, Thread

import pytest
from app.api import approvals
from app.conversations.durable import DurableRunService
from app.conversations.execution import current_execution
from app.conversations.service import ConversationService
from app.graph.state import create_initial_state
from app.storage.models import AgentRun, AgentRunExecution, AgentSession, ApprovalRecord
from app.tests.integration.test_session_api import (
    add_approval_resume_fixture,
    create_test_app_with_database,
)
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver


def _setup(monkeypatch):
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    monkeypatch.setattr(approvals, "build_graph_checkpointer", InMemorySaver)
    app, factory = create_test_app_with_database()
    with factory() as db:
        add_approval_resume_fixture(db)
        db.flush()
        handle = ConversationService(db).resume_turn(tenant_id="tenant_1", run_id="run_1")
        durable = DurableRunService(db)
        durable.prepare(handle=handle, payload={})
        owner = durable.claim(handle.run_id)
        durable.publish_status(handle.run_id, owner)
        db.commit()
    return TestClient(app), factory


def _completed():
    state = create_initial_state(
        session_id="session_1",
        session_key="session_key_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="问题",
        assistant_message_id="assistant_run_1",
    )
    state["request_id"] = "checkpoint_request_1"
    state["trace_id"] = "trace_1"
    state["final_response"] = {
        "content": "已完成审批后任务",
        "cited_evidence_ids": [],
        "approval_status": "approved",
    }
    return state


def _approve(client, decision="approved"):
    return client.post(
        "/api/approvals/approval_1/decision",
        json={"actor_id": "approver_1", "decision": decision},
    )


@pytest.mark.parametrize(
    "decision, terminal", [("approved", "completed"), ("rejected", "rejected")]
)
def test_durable_approval_resume_uses_control_and_publishes_terminal(
    monkeypatch, decision, terminal
):
    client, factory = _setup(monkeypatch)
    dispatches = []

    def dispatch(**kwargs):
        with factory() as db:
            assert db.get(AgentRun, "run_1").status == terminal
        dispatches.append(kwargs)
        return True

    monkeypatch.setattr(approvals, "dispatch_context_compaction", dispatch)

    def resume(**_):
        assert current_execution.get() is not None
        current_execution.get().guard()
        return _completed()

    monkeypatch.setattr(approvals, "resume_question_answer_graph", resume)
    response = _approve(client, decision)
    assert response.status_code == 200, response.text
    with factory() as db:
        assert db.get(AgentRun, "run_1").status == terminal
        execution = db.get(AgentRunExecution, "run_1")
        assert execution.producer_done
        events = DurableRunService(db).events("run_1", after=execution.phase_start_sequence)
        assert events[-1].data["status"] == terminal
        assert any(event.data.get("type") == "final" for event in events)
    assert dispatches == [{
        "tenant_id": "tenant_1", "user_id": "user_1", "session_key": "session_key_1",
    }]
    assert _approve(client, decision).status_code == 200
    assert len(dispatches) == 1


@pytest.mark.parametrize("finish_tool", [False, True])
def test_stop_during_durable_approval_does_not_release_until_safe_boundary(
    monkeypatch, finish_tool
):
    client, factory = _setup(monkeypatch)
    entered, proceed = Event(), Event()
    reached_after_guard = []

    def resume(**_):
        control = current_execution.get()
        receipt_id = None
        if finish_tool and control is not None:
            from app.schemas.tools import ToolCallRequest

            receipt_id = control.tool_started(
                ToolCallRequest(tool_name="write", arguments={}, purpose="写入"), risk_level="high"
            )
        entered.set()
        assert proceed.wait(5)
        if receipt_id:
            control.tool_finished(receipt_id, {"status": "success"})
        control.guard()
        reached_after_guard.append(True)
        return _completed()

    monkeypatch.setattr(approvals, "resume_question_answer_graph", resume)
    responses = []
    worker = Thread(target=lambda: responses.append(_approve(client)))
    worker.start()
    assert entered.wait(5)
    try:
        duplicate = _approve(client)
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "approval_resume_in_progress"
        response = client.post("/api/sessions/session_1/runs/run_1/stop", json={})
        assert response.json()["status"] == "stop_requested"
        with factory() as db:
            assert db.get(AgentSession, "session_key_1").active_run_id == "run_1"
    finally:
        proceed.set()
        worker.join(5)
    assert not worker.is_alive()
    assert reached_after_guard == []
    assert responses[0].status_code == 409
    assert responses[0].json()["detail"]["code"] == "run_cancelled"
    with factory() as db:
        assert db.get(AgentRun, "run_1").status == "cancelled"
        assert db.get(AgentSession, "session_key_1").active_run_id is None
        assert db.get(ApprovalRecord, "approval_1").status == "resume_failed"


def test_durable_approval_failure_remains_retryable(monkeypatch):
    client, factory = _setup(monkeypatch)

    def fail(**_):
        raise RuntimeError("checkpoint temporarily unavailable")

    monkeypatch.setattr(approvals, "resume_question_answer_graph", fail)
    assert _approve(client).status_code == 409
    with factory() as db:
        assert db.get(AgentRun, "run_1").status == "interrupted"
        assert db.get(AgentRunExecution, "run_1").producer_done
    monkeypatch.setattr(approvals, "resume_question_answer_graph", lambda **_: _completed())
    assert _approve(client).status_code == 200


def test_durable_approval_does_not_bypass_unknown_write_barrier(monkeypatch):
    from app.conversations.execution import UnresolvedToolEffect
    from app.storage.models import AgentRunToolReceipt

    client, factory = _setup(monkeypatch)
    with factory() as db:
        db.add(
            AgentRunToolReceipt(
                receipt_id="unknown_write",
                run_id="run_1",
                owner_token="previous",
                tool_name="write",
                risk_level="high",
                status="unknown",
                result_summary={},
            )
        )
        db.commit()

    def resume(**_):
        control = current_execution.get()
        control.ensure_tool_effects_resolved(risk_level="low")
        with pytest.raises(UnresolvedToolEffect):
            control.ensure_tool_effects_resolved(risk_level="high")
        return _completed()

    monkeypatch.setattr(approvals, "resume_question_answer_graph", resume)
    assert _approve(client).status_code == 200


def test_durable_approval_keeps_lock_until_returned_tool_result_is_persisted(monkeypatch):
    from app.conversations.execution import ExecutionControl, retry_pending_tool_results
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt
    from sqlalchemy.exc import SQLAlchemyError

    client, factory = _setup(monkeypatch)

    class FailsOnce:
        kw = factory.kw
        failed = False

        def __call__(self):
            if not self.failed:
                self.failed = True
                raise SQLAlchemyError("transient error")
            return factory()

    def resume(**_):
        control = current_execution.get()
        receipt_id = control.tool_started(
            ToolCallRequest(tool_name="write", arguments={}, purpose="写入"), risk_level="high"
        )
        ExecutionControl(FailsOnce(), control.run_id, control.owner).tool_finished(
            receipt_id, {"status": "success", "result": {"confirmed": True}}
        )
        return _completed()

    monkeypatch.setattr(approvals, "resume_question_answer_graph", resume)
    assert _approve(client).status_code == 503
    with factory() as db:
        assert db.get(AgentRun, "run_1").status == "stop_requested"
        assert not db.get(AgentRunExecution, "run_1").producer_done
        assert db.get(AgentSession, "session_key_1").active_run_id == "run_1"
    assert retry_pending_tool_results(factory.kw["bind"]) == 1
    with factory() as db:
        receipt = db.query(AgentRunToolReceipt).one()
        assert receipt.status == "settled"
        assert receipt.result_summary["result"] == {"confirmed": True}
        assert db.get(AgentRun, "run_1").status == "cancelled"
        assert db.get(AgentSession, "session_key_1").active_run_id is None


def test_durable_approval_locks_execution_before_flushing_run_projection(monkeypatch):
    from app.conversations.execution import ExecutionControl

    client, _ = _setup(monkeypatch)
    guarded_databases = []
    original_guard = ExecutionControl.guard
    original_persist = approvals.persist_agent_run

    def guard(control, db=None):
        original_guard(control, db)
        if db is not None:
            guarded_databases.append(db)

    def persist(**kwargs):
        # stop 的锁序为 execution -> run，收尾 flush run 前也必须先锁 execution。
        assert kwargs["session"] in guarded_databases
        return original_persist(**kwargs)

    monkeypatch.setattr(ExecutionControl, "guard", guard)
    monkeypatch.setattr(approvals, "persist_agent_run", persist)
    monkeypatch.setattr(approvals, "resume_question_answer_graph", lambda **_: _completed())
    assert _approve(client).status_code == 200
