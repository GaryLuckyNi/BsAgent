import json
from threading import Event, Thread

import pytest
from app.api import sessions
from app.graph import runtime
from app.graph.runtime import AgentGraphExecutionError, AgentGraphStreamEvent
from app.graph.state import create_initial_state
from app.storage.models import AgentMessage, AgentRun, AgentRunExecution
from app.tests.integration.test_session_api import (
    _completed_graph_state,
    create_test_app_with_database,
)
from app.tests.support.workflow_llm import WorkflowTestLlm
from app.tools.registry import ToolRegistry
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver


@pytest.fixture(autouse=True)
def isolate_external_dependencies(monkeypatch):
    monkeypatch.setattr(runtime, "get_default_tool_registry", ToolRegistry)
    monkeypatch.setattr(sessions, "build_graph_checkpointer", InMemorySaver)
    monkeypatch.setattr(sessions, "maybe_build_llm_client", WorkflowTestLlm)


def _events(body):
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]


def _submit(client, request_id="durable-1", message="问题"):
    return client.post(
        "/api/sessions/s1/messages/protocol-stream",
        json={
            "input": {"messages": [{"type": "human", "content": message}], "request_id": request_id}
        },
    )


def test_outgoing_progress_and_body_timing_passes_durable_protocol_and_replay(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.conversations import execution

    app, _database = create_test_app_with_database()
    clock = [datetime(2030, 1, 1, tzinfo=UTC)]
    timer_class = execution._PublicEventTiming
    monkeypatch.setattr(execution, "_PublicEventTiming", lambda **kwargs: timer_class(**kwargs, clock=lambda: clock[0]))

    def graph(**kwargs):
        clock[0] += timedelta(milliseconds=5400)
        yield AgentGraphStreamEvent(event="custom", data={"type": "answer_delta", "content": "结论。"})
        yield _completed_graph_state(kwargs, content="结论。")

    monkeypatch.setattr(sessions, "stream_question_answer_graph", graph)
    client = TestClient(app)
    response = _submit(client)
    assert response.status_code == 200
    events = _events(response.text)
    timing = next(event for event in events if isinstance(event, dict) and event.get("type") == "answer_started")
    assert timing["elapsed_ms"] == 5400
    replay = client.get(f"/api/sessions/s1/runs/{timing['run_id']}/events")
    replay_timing = next(event for event in _events(replay.text) if isinstance(event, dict) and event.get("type") == "answer_started")
    assert replay_timing == timing
    assert response.text.index('"answer_started"') < response.text.index('"content": "结论。"')


def test_http_stop_keeps_partial_answer_replays_without_rerun_and_next_turn_works(monkeypatch):
    entered, proceed = Event(), Event()
    invocations = []

    def graph(**kwargs):
        invocations.append(kwargs["request_id"])
        if kwargs["request_id"] == "durable-1":
            yield AgentGraphStreamEvent(
                event="custom", data={"type": "answer_delta", "content": "部分正文"}
            )
            entered.set()
            assert proceed.wait(5)
        else:
            yield AgentGraphStreamEvent(
                event="custom", data={"type": "answer_delta", "content": "新答案"}
            )
        yield _completed_graph_state(kwargs, content="完整正文")

    monkeypatch.setattr(sessions, "stream_question_answer_graph", graph)
    app, database = create_test_app_with_database()
    client = TestClient(app)
    responses = []
    worker = Thread(target=lambda: responses.append(_submit(client)))
    worker.start()
    assert entered.wait(5)
    try:
        active = client.get("/api/sessions/s1/runs/active").json()["run"]
        assert active["status"] == "running"
        # 运行仍开放时正文未完成重放检查；结束/取消后的完整部分正文仍须保留。
        assert active["content"] == ""
        assert active["request_id"] == "durable-1"
        run_id = active["run_id"]
        assert (
            client.post(f"/api/sessions/s1/runs/{run_id}/stop", json={}).json()["status"]
            == "stop_requested"
        )
        assert (
            client.post(f"/api/sessions/s1/runs/{run_id}/stop", json={}).json()["status"]
            == "stop_requested"
        )
    finally:
        proceed.set()
    worker.join(5)
    assert not worker.is_alive()
    assert _events(responses[0].text)[-1]["status"] == "cancelled"
    assert client.get("/api/sessions/s1/runs/active").json() == {"run": None}
    replay = client.get(f"/api/sessions/s1/runs/{run_id}/events?after=1")
    assert replay.status_code == 200
    assert "run_started" not in replay.text
    assert "部分正文" in replay.text
    assert _submit(client).text == responses[0].text
    assert invocations == ["durable-1"]
    with database() as db:
        message = db.get(AgentMessage, active["message_id"])
        assert (message.status, message.content) == ("cancelled", "部分正文")
        assert db.get(AgentRun, run_id).status == "cancelled"
    assert _events(_submit(client, "durable-2", "下一问").text)[-1]["status"] == "completed"
    assert invocations == ["durable-1", "durable-2"]


def test_run_endpoints_enforce_tenant_user_and_session_ownership(monkeypatch):
    monkeypatch.setattr(
        sessions,
        "stream_question_answer_graph",
        lambda **kwargs: iter([_completed_graph_state(kwargs)]),
    )
    app, _ = create_test_app_with_database()
    client = TestClient(app)
    response = _submit(client)
    run_id = _events(response.text)[0]["run_id"]
    for suffix in ("?actor_id=other", "?tenant_id=other"):
        assert client.get(f"/api/sessions/s1/runs/{run_id}/events{suffix}").status_code == 404
        assert client.get(f"/api/sessions/s1/runs/active{suffix}").json() == {"run": None}
    assert client.get(f"/api/sessions/other/runs/{run_id}/events").status_code == 404
    assert (
        client.post(f"/api/sessions/s1/runs/{run_id}/stop", json={"user_id": "other"}).status_code
        == 404
    )
    assert (
        client.post(f"/api/sessions/s1/runs/{run_id}/stop", json={"tenant_id": "other"}).status_code
        == 404
    )
    assert (
        client.post(f"/api/sessions/s1/runs/{run_id}/stop", json={}).json()["status"] == "completed"
    )
    assert (
        "id: 1"
        not in client.get(
            f"/api/sessions/s1/runs/{run_id}/events", headers={"Last-Event-ID": "1"}
        ).text
    )


def test_dispatch_failure_is_terminal_and_late_delivery_cannot_claim(monkeypatch):
    from app.conversations import dispatcher
    from app.conversations.durable import DurableRunService

    def fail(**_):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(dispatcher, "dispatch_turn", fail)
    app, database = create_test_app_with_database()
    client = TestClient(app)
    response = _submit(client)
    assert response.status_code == 503
    run_id = response.json()["detail"]["run_id"]
    assert client.get("/api/sessions/s1/runs/active").json() == {"run": None}
    assert (
        _events(client.get(f"/api/sessions/s1/runs/{run_id}/events").text)[-1]["status"] == "failed"
    )
    with database() as db:
        assert DurableRunService(db).claim(run_id) is None
        assert db.get(AgentRunExecution, run_id).producer_done


def test_snapshot_failure_rolls_back_turn_and_active_pointer(monkeypatch):
    from app.storage.models import AgentSession
    from sqlalchemy import select

    def fail(**_):
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(sessions, "_load_execution_snapshot", fail)
    app, database = create_test_app_with_database()
    client = TestClient(app, raise_server_exceptions=False)
    assert _submit(client).status_code == 500
    assert client.get("/api/sessions/s1/runs/active").json() == {"run": None}
    with database() as db:
        assert db.scalar(select(AgentRun)) is None
        assert db.scalar(select(AgentMessage)) is None
        assert db.scalar(select(AgentSession)) is None


def test_resume_new_phase_does_not_replay_prior_interrupted_terminal(monkeypatch):
    from app.conversations.durable import DurableRunService
    from app.conversations.service import BeginTurn, ConversationService
    from app.graph.state import create_initial_state

    app, database = create_test_app_with_database()
    with database() as db:
        service = ConversationService(db)
        handle = service.begin_turn(
            BeginTurn(
                tenant_id="tenant_1",
                user_id="user_1",
                session_id="s1",
                request_id="resume-phase",
                user_content="选择",
            )
        )
        from app.security.run_budget import initialize_budget
        from app.security.principal import Principal
        initialize_budget(db,handle=handle,principal=Principal("tenant_1","user_1",("agent:invoke",)))
        durable = DurableRunService(db)
        durable.prepare(handle=handle, payload={})
        owner = durable.claim(handle.run_id)
        service.interrupt_turn(tenant_id="tenant_1", run_id=handle.run_id)
        durable.append(
            handle.run_id, owner, "custom", {"type": "input_required", "question": "选择"}
        )
        durable.publish_status(handle.run_id, owner)
        db.commit()
        old_sequence = db.get(AgentRunExecution, handle.run_id).last_sequence

    def resumed(**_):
        state = create_initial_state(
            tenant_id="tenant_1",
            user_id="user_1",
            session_id="s1",
            session_key=handle.session_key,
            request_id=handle.request_id,
            trace_id=handle.trace_id,
            user_message="选择",
            user_message_id=handle.user_message_id,
            assistant_message_id=handle.assistant_message_id,
        )
        state["final_response"] = {
            "content": "已选择",
            "trace_id": handle.trace_id,
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "resume_user_input_graph", resumed)
    response = TestClient(app).post(
        "/api/sessions/s1/messages/protocol-stream",
        json={
            "command": {
                "resume": {
                    "interrupt_id": "i",
                    "interaction_id": "c",
                    "input_type": "selection",
                    "value": "v",
                }
            }
        },
    )
    assert response.status_code == 200
    events = _events(response.text)
    assert events[0]["type"] == "run_started"
    assert events[-1]["status"] == "completed"
    assert '"status": "interrupted"' not in response.text
    assert "input_required" not in response.text
    with database() as db:
        assert db.get(AgentRunExecution, handle.run_id).phase_start_sequence == old_sequence


def _pending_input_turn(database):
    from app.conversations.durable import DurableRunService
    from app.conversations.service import BeginTurn, ConversationService

    with database() as db:
        service = ConversationService(db)
        handle = service.begin_turn(BeginTurn(
            tenant_id="tenant_1", user_id="user_1", session_id="s1",
            request_id="pending-input", user_content="选择数据来源",
        ))
        from app.security.run_budget import initialize_budget
        from app.security.principal import Principal
        initialize_budget(db,handle=handle,principal=Principal("tenant_1","user_1",("agent:invoke",)))
        durable = DurableRunService(db)
        durable.prepare(handle=handle, payload={})
        owner = durable.claim(handle.run_id)
        service.interrupt_turn(tenant_id="tenant_1", run_id=handle.run_id)
        durable.publish_status(handle.run_id, owner)
        db.commit()
    return handle


def _resume_input(client):
    return client.post("/api/sessions/s1/messages/protocol-stream", json={
        "command": {"resume": {
            "interrupt_id": "interrupt", "interaction_id": "interaction",
            "input_type": "selection", "value": "web_search",
        }},
    })


def test_catalog_change_during_input_resume_releases_session_for_next_query(monkeypatch):
    from app.graph.nodes.user_input_gate import user_input_gate_node
    from app.graph.state import AgentState, build_checkpoint_thread_id
    from app.schemas.user_input import UserInputOption, UserInputRequest, UserInputSource
    from app.tests.unit.test_request_flow import Answers, intent
    from langgraph.graph import END, StateGraph

    app, database = create_test_app_with_database()
    handle = _pending_input_turn(database)
    state = create_initial_state(
        tenant_id="tenant_1", user_id="user_1", session_id="s1",
        session_key=handle.session_key, request_id=handle.request_id,
        trace_id=handle.trace_id, user_message="选择数据来源",
        user_message_id=handle.user_message_id,
        assistant_message_id=handle.assistant_message_id,
        tool_catalog_digest="catalog-before-disabling-source",
    )
    state["request_flow_version"] = "context-routing-v1"
    state["intent"] = intent(goal="读取事实")
    state["plan"] = {"tasks": [{"id": "facts", "task_goal": "读取事实", "execution_type": "tool"}]}
    state["planner_iteration"] = 1
    state["pending_user_input"] = UserInputRequest(
        interaction_id="interaction", input_type="selection", question="选择来源",
        options=(UserInputOption(value="web_search", label="搜索"), UserInputOption(value="existing", label="已有资料")),
        source=UserInputSource(node="tool_agent", kind="choice", task_id="facts"),
    )
    saver = InMemorySaver()
    graph = StateGraph(AgentState)
    graph.add_node("user_input_gate", user_input_gate_node)
    graph.set_entry_point("user_input_gate")
    graph.add_edge("user_input_gate", END)
    paused = graph.compile(checkpointer=saver).invoke(state, {"configurable": {"thread_id": build_checkpoint_thread_id(
        tenant_id="tenant_1", session_key=handle.session_key, request_id=handle.request_id,
    )}})
    interrupt_id = paused["__interrupt__"][0].id
    llm = Answers({"action": "answer", "answer": "本轮未取得新资料，现有信息不足以给出完整结果。"})
    monkeypatch.setattr(sessions, "build_graph_checkpointer", lambda: saver)
    monkeypatch.setattr(sessions, "maybe_build_llm_client", lambda: llm)
    monkeypatch.setattr(sessions, "stream_question_answer_graph", lambda **kwargs: iter([
        _completed_graph_state(kwargs, content="新一轮正常完成"),
    ]))
    client = TestClient(app)

    resumed = client.post("/api/sessions/s1/messages/protocol-stream", json={"command": {"resume": {
        "interrupt_id": interrupt_id, "interaction_id": "interaction",
        "input_type": "selection", "value": "web_search",
    }}})
    assert resumed.status_code == 200
    assert "tool_catalog_changed" in llm.calls[0]["user_prompt"]
    next_query = _submit(client, "after-catalog-change", "下一问")
    assert next_query.status_code == 200, next_query.text
    assert _events(resumed.text)[-1]["status"] == "completed"
    assert _events(next_query.text)[-1]["status"] == "completed"
    assert client.get("/api/sessions/s1/runs/active").json() == {"run": None}
    with database() as db:
        assert db.get(AgentRun, handle.run_id).status == "completed"
        assistant = db.get(AgentMessage, handle.assistant_message_id)
        assert assistant.status == "completed"
        assert db.get(AgentRunExecution, handle.run_id).producer_done
    replay = client.get(f"/api/sessions/s1/runs/{handle.run_id}/events")
    assert _events(replay.text)[-1]["status"] == "completed"


def test_transient_input_resume_failure_keeps_original_run_retryable(monkeypatch):
    app, database = create_test_app_with_database()
    handle = _pending_input_turn(database)

    def unavailable(**kwargs):
        raise AgentGraphExecutionError(
            node_name="model", message="模型暂时不可用", code="llm_timeout", http_status=504,
        )

    monkeypatch.setattr(sessions, "resume_user_input_graph", unavailable)
    client = TestClient(app)
    response = _resume_input(client)
    assert _events(response.text)[-1]["status"] == "interrupted"
    assert client.get("/api/sessions/s1/runs/active").json()["run"]["run_id"] == handle.run_id

    def recovered(**kwargs):
        state = create_initial_state(
            tenant_id="tenant_1", user_id="user_1", session_id="s1",
            session_key=handle.session_key, request_id=handle.request_id,
            trace_id=handle.trace_id, user_message="选择数据来源",
            user_message_id=handle.user_message_id,
            assistant_message_id=handle.assistant_message_id,
        )
        state["final_response"] = {
            "content": "恢复成功", "trace_id": handle.trace_id,
            "cited_evidence_ids": [], "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "resume_user_input_graph", recovered)
    retry = _resume_input(client)
    assert _events(retry.text)[-1]["status"] == "completed"
    assert client.get("/api/sessions/s1/runs/active").json() == {"run": None}
