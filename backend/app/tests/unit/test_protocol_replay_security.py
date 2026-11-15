"""持久化事件使用真实仓储和合成数据验证重放出口。"""

import json

import pytest
from app.conversations.durable import DurableRunService
from app.conversations.execution import subscribe_events
from app.conversations.service import BeginTurn, ConversationService
from app.storage.models import AgentMessage, AgentRun, AgentRunExecution
from app.tests.integration.test_session_api import create_test_app_with_database
from fastapi.testclient import TestClient

CANARY = "REPLAY_CANARY_987654321"
SECRET = '{"password":"' + CANARY + '"}'


def seeded_run(chunks, *, done=True, tail=()):
    app, factory = create_test_app_with_database()
    with factory() as db:
        handle = ConversationService(db).begin_turn(
            BeginTurn(
                tenant_id="tenant_1",
                user_id="user_1",
                session_id="s1",
                request_id="replay-security",
                user_content="公开问题",
            )
        )
        durable = DurableRunService(db)
        durable.prepare(handle=handle, payload={})
        owner = durable.claim(handle.run_id)
        for chunk in chunks:
            durable.append(
                handle.run_id,
                owner,
                "messages",
                [
                    {"id": handle.assistant_message_id, "type": "ai", "content": chunk},
                    {"langgraph_node": "synthesizer"},
                ],
            )
        for event, data in tail:
            durable.append(handle.run_id, owner, event, data)
        if done:
            ConversationService(db).complete_turn(
                tenant_id="tenant_1",
                run_id=handle.run_id,
                content="".join(chunks),
            )
            durable.publish_status(handle.run_id, owner)
        db.commit()
    return app, factory, handle


def decoded_frames(frames):
    result = []
    for frame in frames:
        if frame.startswith(":"):
            continue
        fields = dict(line.split(":", 1) for line in frame.splitlines() if ":" in line)
        result.append(
            (
                int(fields["id"]) if "id" in fields else None,
                fields["event"].strip(),
                json.loads(fields["data"]),
            )
        )
    return result


def test_producer_publishes_checked_prefix_before_provider_finishes():
    from app.conversations.execution import produce_frames
    _app, factory = create_test_app_with_database()
    with factory() as db:
        handle = ConversationService(db).begin_turn(BeginTurn(
            tenant_id="tenant_1", user_id="user_1", session_id="s1",
            request_id="live-safe-stream", user_content="公开问题",
        ))
        DurableRunService(db).prepare(handle=handle, payload={})
        db.commit()
    text = "公开财报收入同比增长，现金流保持稳定。" * 8
    observed = []
    def source(db):
        payload = [{"id": handle.assistant_message_id, "type": "ai", "content": text}, {}]
        yield "event: messages\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        subscription = subscribe_events(session_factory=factory, run_id=handle.run_id)
        try:
            for _ in range(3):
                frame = next(subscription)
                if 'event: messages' in frame:
                    observed.append(frame)
                    break
                if frame.startswith(":"):
                    break
        finally:
            subscription.close()
        ConversationService(db).complete_turn(tenant_id="tenant_1", run_id=handle.run_id, content=text)
        db.commit()
        yield 'event: custom\ndata: {"type":"final","response":{"content":' + json.dumps(text) + '}}\n\n'
    produce_frames(session_factory=factory, run_id=handle.run_id, frames_factory=source)
    assert observed and "公开财报" in observed[0], "不能等producer_done才展示已检查正文"
    frames = decoded_frames(subscribe_events(session_factory=factory, run_id=handle.run_id))
    assert "".join(data[0]["content"] for _, event, data in frames if event == "messages") == text


def test_untrusted_persisted_metadata_cannot_claim_checked_content():
    _app, factory, handle = seeded_run([SECRET], done=False)
    with factory() as db:
        execution = db.get(AgentRunExecution, handle.run_id)
        from app.security.model_output import _output_policy
        DurableRunService(db).append(handle.run_id, execution.owner_token, "messages", [
            {"id": handle.assistant_message_id, "content": "公开事实"},
            {"security_content_policy": _output_policy().version},
        ])
        db.commit()
        row = DurableRunService(db).events(handle.run_id, after=2)[0]
        assert "security_content_policy" not in row.data[1]


@pytest.mark.parametrize("after", [0, 3, 210])
@pytest.mark.parametrize(
    "secret",
    [
        SECRET,
        "Bearer " + CANARY,
        "Cookie: sid=" + CANARY,
        "-----BEGIN PRIVATE KEY-----\n" + CANARY + "\n-----END PRIVATE KEY-----",
        "https://example.com/#/login?auth=" + CANARY,
    ],
)
def test_legacy_credentials_cannot_escape_through_pages_or_middle_cursor(after, secret):
    text = "公开事实。" * 40 + secret
    _app, factory, handle = seeded_run(list(text))
    frames = decoded_frames(
        subscribe_events(session_factory=factory, run_id=handle.run_id, after=after)
    )
    body = "".join(data[0]["content"] for _, event, data in frames if event == "messages")
    assert CANARY not in body
    assert CANARY[-8:] not in body
    assert frames[-1][2]["status"] == "completed"
    assert all(sequence > after for sequence, _, _ in frames)
    with factory() as db:
        assert CANARY in db.get(AgentMessage, handle.assistant_message_id).content
        assert db.get(AgentRun, handle.run_id).status == "completed"


def test_normal_replay_retains_sequence_identity_chunks_and_source_object():
    chunks = ["公开事实。", "600519.SH 2026 年。"]
    _app, factory, handle = seeded_run(chunks)
    frames = decoded_frames(subscribe_events(session_factory=factory, run_id=handle.run_id))
    messages = [(sequence, data) for sequence, event, data in frames if event == "messages"]
    assert [data[0]["content"] for _, data in messages] == chunks
    assert [sequence for sequence, _ in messages] == [2, 3]
    assert all(data[0]["id"] == handle.assistant_message_id for _, data in messages)


def test_active_endpoint_holds_open_body_and_projects_sealed_legacy_content():
    app, factory, handle = seeded_run([SECRET], done=False)
    client = TestClient(app)
    active = client.get("/api/sessions/s1/runs/active").json()["run"]
    assert active["status"] == "running" and active["content"] == ""
    with factory() as db:
        db.get(AgentRunExecution, handle.run_id).producer_done = True
        db.get(AgentRun, handle.run_id).status = "interrupted"
        db.commit()
    active = client.get("/api/sessions/s1/runs/active").json()["run"]
    assert CANARY not in active["content"]
    assert active["status"] == "interrupted"


def test_open_subscription_sends_only_progress_preview_then_checked_body_in_order():
    _app, factory, handle = seeded_run(
        ['{"password":"'],
        done=False,
        tail=[
            (
                "custom",
                {"type": "progress", "id": "task:1", "message": "继续处理", "status": "started"},
            ),
        ],
    )
    stream = subscribe_events(session_factory=factory, run_id=handle.run_id)
    first = decoded_frames([next(stream)])[0]
    preview = decoded_frames([next(stream)])[0]
    assert first[0] == 1
    assert preview[0] is None and preview[2]["type"] == "progress"
    with factory() as db:
        execution = db.get(AgentRunExecution, handle.run_id)
        durable = DurableRunService(db)
        durable.append(
            handle.run_id,
            execution.owner_token,
            "messages",
            [
                {"id": handle.assistant_message_id, "type": "ai", "content": CANARY + '"}'},
                {},
            ],
        )
        ConversationService(db).complete_turn(
            tenant_id="tenant_1", run_id=handle.run_id, content=SECRET
        )
        durable.publish_status(handle.run_id, execution.owner_token)
        db.commit()
    result = decoded_frames(list(stream))
    assert [sequence for sequence, _, _ in result] == [2, 3, 4, 5]
    assert CANARY not in "".join(
        data[0]["content"] for _, event, data in result if event == "messages"
    )
    assert result[-1][2]["status"] == "completed"


def test_message_ids_do_not_split_the_recovery_clients_concatenation_boundary():
    from app.storage.models import AgentRunProtocolEvent
    from sqlalchemy import select

    _app, factory, handle = seeded_run(['{"password":"', CANARY + '"}'])
    with factory() as db:
        row = db.scalar(
            select(AgentRunProtocolEvent).where(
                AgentRunProtocolEvent.run_id == handle.run_id,
                AgentRunProtocolEvent.sequence == 3,
            )
        )
        row.data = [{**row.data[0], "id": "another-message"}, {}]
        db.commit()
    frames = decoded_frames(subscribe_events(session_factory=factory, run_id=handle.run_id))
    assert CANARY not in "".join(
        data[0]["content"] for _, event, data in frames if event == "messages"
    )


def test_old_custom_final_progress_sources_and_errors_use_the_shared_public_floor():
    _app, factory, handle = seeded_run(
        ["公开事实"],
        tail=[
            ("custom", {"type": "progress", "id": "step:1", "message": SECRET}),
            (
                "custom",
                {
                    "type": "answer_sources",
                    "sources": [
                        {"source_id": "source_7", "url": "https://example.com/?auth=" + CANARY},
                    ],
                },
            ),
            (
                "custom",
                {
                    "type": "final",
                    "response": {
                        "content": SECRET,
                        "trace_id": "trace-1",
                        "sources": [
                            {"source_id": "source_7", "url": "https://example.com/?auth=" + CANARY},
                        ],
                    },
                },
            ),
            ("error", {"code": "fixed_error", "message": SECRET}),
        ],
    )
    frames = decoded_frames(subscribe_events(session_factory=factory, run_id=handle.run_id))
    assert CANARY not in str(frames)
    assert frames[-1][2]["status"] == "completed"
    final = next(
        data for _, event, data in frames if event == "custom" and data.get("type") == "final"
    )
    assert final["response"]["trace_id"] == "trace-1"
    assert final["response"]["sources"] == []


@pytest.mark.parametrize("failure", ["checker", "characters", "events"])
def test_replay_fault_or_budget_withholds_body_without_changing_completion(monkeypatch, failure):
    from app.security import protocol_replay
    from app.security.tool_content import ToolContentPolicy

    _app, factory, handle = seeded_run(list(SECRET))
    if failure == "checker":

        def fail(*args, **kwargs):
            raise RuntimeError(SECRET)

        monkeypatch.setattr(ToolContentPolicy, "project", fail)
    else:
        monkeypatch.setattr(
            protocol_replay,
            "MAX_REPLAY_BODY_CHARS" if failure == "characters" else "MAX_REPLAY_EVENTS",
            2,
        )
    frames = decoded_frames(subscribe_events(session_factory=factory, run_id=handle.run_id))
    assert CANARY not in str(frames)
    assert frames[-1][2]["status"] == "completed"
    with factory() as db:
        assert db.get(AgentMessage, handle.assistant_message_id).content == SECRET
        assert db.get(AgentRun, handle.run_id).status == "completed"


def test_http_reconnect_uses_prefix_context_before_last_event_id():
    app, _factory, handle = seeded_run(list("公开事实。" * 40 + SECRET))
    response = TestClient(app).get(
        f"/api/sessions/s1/runs/{handle.run_id}/events?after=1",
        headers={"Last-Event-ID": "210"},
    )
    assert response.status_code == 200
    frames = decoded_frames(response.text.split("\n\n")[:-1])
    assert all(sequence > 210 for sequence, _, _ in frames)
    assert CANARY not in "".join(
        data[0]["content"] for _, event, data in frames if event == "messages"
    )


def test_missing_execution_does_not_leave_replay_waiting_forever(monkeypatch):
    from app.conversations import execution

    _app, factory, handle = seeded_run(list(SECRET))
    with factory() as db:
        db.delete(db.get(AgentRunExecution, handle.run_id))
        db.commit()
    calls = [0]
    original_events = DurableRunService.events

    def bounded_events(self, *args, **kwargs):
        calls[0] += 1
        assert calls[0] <= 2, "已无执行句柄的重放不得反复重新读取同一批"
        return original_events(self, *args, **kwargs)

    monkeypatch.setattr(DurableRunService, "events", bounded_events)

    def no_wait(_seconds):
        pytest.fail("已无执行句柄的重放不应等待生产者")

    monkeypatch.setattr(execution, "sleep", no_wait)
    frames = decoded_frames(subscribe_events(session_factory=factory, run_id=handle.run_id))
    assert CANARY not in str(frames)


def test_active_body_and_seal_are_read_from_one_snapshot_during_completion(monkeypatch):
    from app.conversations.repository import ConversationRepository

    prefix = "sk-123456789012345"
    complete = prefix + "678901234567890"
    app, factory, handle = seeded_run([prefix], done=False)
    original = ConversationRepository.get_assistant_by_run
    calls = []

    def finish_between_reads(self, *args, **kwargs):
        assistant = original(self, *args, **kwargs)
        if not calls:
            calls.append(True)
            with factory() as db:
                db.get(AgentMessage, handle.assistant_message_id).content = complete
                db.get(AgentMessage, handle.assistant_message_id).status = "interrupted"
                db.get(AgentRun, handle.run_id).status = "interrupted"
                db.get(AgentRunExecution, handle.run_id).producer_done = True
                db.commit()
        return assistant

    monkeypatch.setattr(ConversationRepository, "get_assistant_by_run", finish_between_reads)
    active = TestClient(app).get("/api/sessions/s1/runs/active").json()["run"]
    assert calls == [True]
    assert prefix not in active["content"]
    assert active["status"] == "interrupted"


def test_history_list_search_and_preview_do_not_publish_open_legacy_prefix():
    prefix = "sk-123456789012345"
    app, _factory, handle = seeded_run([prefix], done=False)
    client = TestClient(app)
    messages = client.get("/api/sessions/s1/messages").json()["items"]
    assistant = next(row for row in messages if row["message_id"] == handle.assistant_message_id)
    assert assistant["content"] == "" and assistant["status"] == "streaming"
    assert prefix not in client.get("/api/sessions").text
    assert client.get("/api/sessions/search", params={"q": prefix}).json()["items"] == []


def test_session_preview_checks_full_sealed_text_before_sql_length_clipping():
    from app.security.public_output import public_text

    prefix = "sk-123456789012345"
    complete = prefix + "678901234567890"
    padding = "A" * 304 + " "
    # 旧 SQL 截断只留下低于已知 key 长度门槛的片段；完整检查可以识别整份 key。
    assert "sk-" in public_text((padding + complete)[:320])
    app, _factory, _handle = seeded_run([padding + complete])
    preview = TestClient(app).get("/api/sessions").json()["items"][0]["preview"]
    assert "sk-" not in preview
    assert preview.startswith(padding) and len(preview) <= 320


def test_resumed_phase_resets_body_assembly_without_changing_message_ids():
    _app, factory, handle = seeded_run(
        ['{"password":"'],
        tail=[
            ("custom", {"type": "run_started", "run_id": "same-run-phase"}),
            (
                "messages",
                [{"type": "ai", "id": "resumed-message", "content": "公开事实 600519.SH"}, {}],
            ),
        ],
    )
    frames = decoded_frames(subscribe_events(session_factory=factory, run_id=handle.run_id))
    chunks = [data[0] for _, event, data in frames if event == "messages"]
    assert chunks[0]["id"] == handle.assistant_message_id
    assert chunks[1] == {"type": "ai", "id": "resumed-message", "content": "公开事实 600519.SH"}


def test_closing_open_subscription_does_not_hold_database_connection():
    _app, factory, handle = seeded_run(["未封口正文"], done=False)
    stream = subscribe_events(session_factory=factory, run_id=handle.run_id)
    assert decoded_frames([next(stream)])[0][2]["type"] == "run_started"
    stream.close()
    assert factory.kw["bind"].pool.checkedout() == 0


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_terminal_partial_body_is_checked_without_rewriting_its_real_status(status):
    app, factory, handle = seeded_run(['{"password":"', CANARY], done=False)
    with factory() as db:
        owner = db.get(AgentRunExecution, handle.run_id).owner_token
        durable = DurableRunService(db)
        service = ConversationService(db)
        if status == "cancelled":
            durable.request_stop(handle.run_id)
            durable.finish_cancelled(handle.run_id, owner)
        else:
            if status == "failed":
                service.fail_turn(
                    tenant_id="tenant_1", run_id=handle.run_id, error_code="fixed_failure"
                )
            else:
                service.interrupt_turn(tenant_id="tenant_1", run_id=handle.run_id)
            durable.publish_status(handle.run_id, owner)
        db.commit()
    response = TestClient(app).get(f"/api/sessions/s1/runs/{handle.run_id}/events?after=1")
    assert response.status_code == 200
    frames = decoded_frames(response.text.split("\n\n")[:-1])
    assert CANARY not in str(frames) and CANARY[-8:] not in str(frames)
    assert frames[-1][2]["status"] == status
    with factory() as db:
        message = db.get(AgentMessage, handle.assistant_message_id)
        assert message.content.endswith(CANARY) and message.status == status
