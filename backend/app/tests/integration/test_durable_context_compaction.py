"""持久化执行与上下文压缩的集成边界。"""

from threading import Event, Thread

import pytest
from app.api import sessions
from app.context_compaction.contracts import ContextPreparationEvent, EffectiveContext
from app.conversations import dispatcher
from app.conversations.durable import DurableRunService
from app.storage.models import AgentMessage, AgentRun, AgentRunExecution, AgentSession
from app.tests.integration.test_durable_run_api import _events, _submit
from app.tests.integration.test_session_api import (
    _completed_graph_state,
    create_test_app_with_database,
)
from app.tests.support.workflow_llm import WorkflowTestLlm
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(sessions.settings, "context_compaction_enabled", True)
    monkeypatch.setattr(sessions, "build_graph_checkpointer", InMemorySaver)
    monkeypatch.setattr(sessions, "maybe_build_llm_client", WorkflowTestLlm)
    monkeypatch.setattr(sessions, "dispatch_context_compaction", lambda **_: True)


def test_protocol_persists_compaction_progress_before_prepare_and_replays_once(monkeypatch):
    calls = []
    app, factory = create_test_app_with_database()

    class Preparation:
        def prepare_for_request_events(self, **kwargs):
            calls.append("prepare")
            with factory() as db:
                assert db.get(AgentSession, kwargs["session_key"]) is not None
                run = db.scalar(select(AgentRun))
                assert db.get(AgentRunExecution, run.run_id) is not None
            yield ContextPreparationEvent(stage="compacting_context")
            with factory() as db:
                events = DurableRunService(db).events(run.run_id, after=0)
                assert any(item.data.get("stage") == "compacting_context" for item in events)
            calls.append("summary")
            return EffectiveContext((), "cmp-durable", "compacted", 321)

    def graph(**kwargs):
        calls.append("qa")
        yield _completed_graph_state(kwargs)

    monkeypatch.setattr(sessions, "build_context_compaction_service", lambda **_: Preparation())
    monkeypatch.setattr(sessions, "stream_question_answer_graph", graph)
    client = TestClient(app)
    response = _submit(client)
    assert response.status_code == 200, response.text
    events = _events(response.text)
    assert events[-1]["status"] == "completed", response.text
    assert calls == ["prepare", "summary", "qa"]
    run_id = events[0]["run_id"]
    assert "compacting_context" in client.get(f"/api/sessions/s1/runs/{run_id}/events").text
    assert _submit(client).text == response.text
    assert calls == ["prepare", "summary", "qa"]
    with factory() as db:
        execution = db.get(AgentRunExecution, run_id)
        assert execution.payload["context_status"] == "full"
        assistant = db.get(AgentMessage, events[0]["message_id"])
        assert assistant.metadata_json["context_compaction_id"] == "cmp-durable"


def test_enabled_http_snapshot_failure_rolls_back_entire_turn(monkeypatch):
    original = sessions._load_execution_snapshot

    def fail_after_snapshot(**kwargs):
        original(**kwargs)
        raise RuntimeError("snapshot failed after attachment materialization")

    monkeypatch.setattr(sessions, "_load_execution_snapshot", fail_after_snapshot)
    app, factory = create_test_app_with_database()
    assert _submit(TestClient(app, raise_server_exceptions=False)).status_code == 500
    with factory() as db:
        for model in (AgentSession, AgentRun, AgentMessage, AgentRunExecution):
            assert db.scalar(select(model)) is None


def test_stop_during_compaction_prepare_never_enters_qa(monkeypatch):
    entered, proceed = Event(), Event()
    qa_calls = []

    class Preparation:
        def prepare_for_request_events(self, **_):
            yield ContextPreparationEvent(stage="compacting_context")
            entered.set()
            assert proceed.wait(5)
            return EffectiveContext((), None, "full", 1)

    def graph(**kwargs):
        qa_calls.append(kwargs)
        yield _completed_graph_state(kwargs)

    monkeypatch.setattr(sessions, "build_context_compaction_service", lambda **_: Preparation())
    monkeypatch.setattr(sessions, "stream_question_answer_graph", graph)
    app, _ = create_test_app_with_database()
    client = TestClient(app)
    responses = []
    worker = Thread(target=lambda: responses.append(_submit(client)))
    worker.start()
    try:
        assert entered.wait(5)
        run = client.get("/api/sessions/s1/runs/active").json()["run"]
        assert client.post(f"/api/sessions/s1/runs/{run['run_id']}/stop", json={}).status_code == 200
    finally:
        proceed.set()
        worker.join(5)
    assert not worker.is_alive()
    assert _events(responses[0].text)[-1]["status"] == "cancelled"
    assert qa_calls == []


def test_old_payload_executes_with_default_compaction_metadata(monkeypatch):
    pending = []
    monkeypatch.setattr(dispatcher, "dispatch_turn", lambda **kwargs: pending.append(kwargs))
    app, factory = create_test_app_with_database()
    # 返回空订阅只用于捕获原始 payload，随后使用真实 producer 执行。
    from app.api import runs
    monkeypatch.setattr(runs, "_response", lambda *_args, **_kwargs: {"queued": True})
    assert _submit(TestClient(app)).status_code == 200
    with factory() as db:
        execution = db.scalar(select(AgentRunExecution))
        payload = dict(execution.payload)
        for field in ("context_compaction_id", "context_status", "projected_context_tokens"):
            payload.pop(field, None)
        payload["principal"] = dict(payload["principal"])
        payload["principal"].pop("expires_at", None)
        execution.payload = payload
        db.commit()
        run_id = execution.run_id
    monkeypatch.setattr(sessions.settings, "context_compaction_enabled", False)
    monkeypatch.setattr(sessions, "stream_question_answer_graph", lambda **kw: iter([_completed_graph_state(kw)]))
    dispatcher.execute_local_turn(session_factory=factory, run_id=run_id, memory_store=object())
    with factory() as db:
        assert db.get(AgentRun, run_id).status == "completed"


@pytest.mark.parametrize("enabled", [False, True])
def test_twenty_long_history_messages_respect_budget_and_stream_compaction_notice(monkeypatch, enabled):
    import json

    from app.context_compaction.policy import ConservativeTokenEstimator
    from app.conversations.service import BeginTurn, ConversationService
    from app.llm.schemas import LlmCompletionResult

    monkeypatch.setattr(sessions, "CONVERSATION_HISTORY_LIMIT", 20)
    monkeypatch.setattr(sessions.settings, "context_compaction_enabled", enabled)
    monkeypatch.setattr(sessions.settings, "llm_context_window_tokens", 8192)
    monkeypatch.setattr(sessions.settings, "context_non_history_reserve_tokens", 1024)
    monkeypatch.setattr(sessions.settings, "context_compaction_summary_max_tokens", 300)

    class SummaryClient(WorkflowTestLlm):
        async def complete(self, **kwargs):
            assert "上下文摘要器" in kwargs["system_prompt"]
            summary = {
                "goal": "继续分析公司经营情况", "constraints": [], "decisions": [],
                "open_items": [], "facts": ["已完成历史分析"], "evidence_refs": [],
                "user_preferences": [], "superseded": [],
            }
            return LlmCompletionResult(content=json.dumps(summary, ensure_ascii=False), model="test", provider="test")

    monkeypatch.setattr(sessions, "maybe_build_llm_client", SummaryClient)
    app, factory = create_test_app_with_database()
    with factory() as db:
        service = ConversationService(db)
        for number in range(10):
            handle = service.begin_turn(BeginTurn(
                tenant_id="tenant_1", user_id="user_1", session_id="s1",
                request_id=f"long-history-{number}", user_content="分析公司经营情况",
            ))
            service.complete_turn(tenant_id="tenant_1", run_id=handle.run_id,
                                  content="历史分析结论。" * 160)
        db.commit()
    histories = []

    def graph(**kwargs):
        history = kwargs["history"]
        histories.append(history)
        estimator = ConservativeTokenEstimator()
        projected = sum(estimator.message_tokens(m.role, m.content) for m in history) + 1100
        assert projected <= 8192, "未压缩的长历史进入了模型执行链路"
        yield _completed_graph_state(kwargs)

    monkeypatch.setattr(sessions, "stream_question_answer_graph", graph)
    response = _submit(TestClient(app), "after-twenty", "继续分析")
    events = _events(response.text)
    assert events[-1]["status"] == "completed", response.text
    assert histories and len(histories[0]) < 20
    notices = [event for event in events if isinstance(event, dict) and event.get("stage") == "compacting_context"]
    assert bool(notices) is enabled
    if enabled:
        assert histories[0][0].metadata.get("context_compaction") is True
        assert notices[0]["message"] == "正在压缩上下文"
        run_id = events[0]["run_id"]
        assert "compacting_context" in TestClient(app).get(f"/api/sessions/s1/runs/{run_id}/events").text


@pytest.mark.parametrize("enabled", [False, True])
def test_history_limit_zero_preserves_cancelled_handoff_with_compaction(monkeypatch, enabled):
    from app.conversations.service import BeginTurn, ConversationService

    monkeypatch.setattr(sessions.settings, "context_compaction_enabled", enabled)
    monkeypatch.setattr(sessions, "CONVERSATION_HISTORY_LIMIT", 0)
    app, factory = create_test_app_with_database()
    with factory() as db:
        handle = ConversationService(db).begin_turn(BeginTurn(
            tenant_id="tenant_1", user_id="user_1", session_id="s1",
            request_id="previous-stopped", user_content="上一轮未完成的问题",
        ))
        db.get(AgentMessage, handle.assistant_message_id).status = "cancelled"
        db.get(AgentMessage, handle.assistant_message_id).content = "上轮停止时的部分答案"
        db.get(AgentRun, handle.run_id).status = "cancelled"
        db.get(AgentSession, handle.session_key).active_run_id = None
        db.commit()
    histories = []

    def graph(**kwargs):
        histories.append(kwargs["history"])
        yield _completed_graph_state(kwargs)

    monkeypatch.setattr(sessions, "stream_question_answer_graph", graph)
    response = _submit(TestClient(app), "next-after-stop", "继续回答")
    assert _events(response.text)[-1]["status"] == "completed", response.text
    handoffs = [message for message in histories[0]
                if message.metadata.get("source") == "cancelled_turn_handoff"]
    assert len(handoffs) == 1
    assert "上一轮未完成的问题" in handoffs[0].content
    assert "上轮停止时的部分答案" in handoffs[0].content
