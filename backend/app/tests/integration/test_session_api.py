import json
import re
from datetime import UTC, datetime

from fastapi import Request
from fastapi.testclient import TestClient
import pytest
from langgraph.types import Interrupt
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import approvals, sessions
from app.conversations.service import BeginTurn, ConversationService
from app.graph.state import create_initial_state
from app.graph.runtime import AgentGraphExecutionError
from app.harness.tracing import InMemoryTraceStore, TraceEvent
from app.llm.deepseek import LlmConfigurationError
from app.llm.schemas import LlmCompletionResult
from app.main import create_app
from app.schemas.approvals import ApprovalRequest, ApprovalStatus
from app.schemas.approvals import ReadyToolCall
from app.schemas.memory import MemoryCandidate
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult
from app.security.dependencies import get_current_principal
from app.security.principal import Principal
from app.storage.database import get_db
from app.storage.models import (
    AgentRun,
    AgentMessage,
    AgentSession,
    AgentTraceEvent,
    ApprovalRecord,
    Base,
    ExperienceEntryRecord,
)


class PreferenceResponseLlmClient:
    async def complete(self, **kwargs):
        if "意图分类器" in kwargs.get("system_prompt", ""):
            return LlmCompletionResult(
                content="""
                {
                  "intent": "conversation",
                  "domain": "preference_ack",
                  "sub_intent": "language_preference",
                  "candidate_skill": null,
                  "candidate_tool": null,
                  "risk_level": "low",
                  "confidence": 0.9,
                  "needs_clarification": false,
                  "reason_codes": ["classifier_selected_conversation"]
                }
                """,
                model="fake-intent-model",
                provider="fake",
            )
        return LlmCompletionResult(
            content="我会继续使用中文回答。",
            model="fake-render-model",
            provider="fake",
        )


class StreamingNarrativeLlmClient:
    async def complete(self, **_):
        return LlmCompletionResult(
            content="兜底回答不应该用于思考流。",
            model="fake-stream-model",
            provider="fake",
        )

    async def stream_complete(self, **_):
        for chunk in ["我先识别你的投研目标，", "再检查可用市场数据。"]:
            yield chunk


class IncompleteStreamingNarrativeLlmClient:
    async def complete(self, **_):
        return LlmCompletionResult(content="unused", model="fake", provider="fake")

    async def stream_complete(self, **_):
        chunks = [
            "我会先拆解“分析银之杰”这个问题，明确这是A股公司分析请求。",
            "我会检查公司资料、行情数据和公开资料。",
            "最后提示基本面变化或市场系统性风险可能带来的",
        ]
        for chunk in chunks:
            yield chunk


class FailingStreamingNarrativeLlmClient:
    async def complete(self, **_):
        return LlmCompletionResult(content="unused", model="fake", provider="fake")

    async def stream_complete(self, **_):
        raise RuntimeError("thinking stream unavailable")
        yield ""


@pytest.fixture(autouse=True)
def avoid_live_llm_provider(monkeypatch):
    monkeypatch.setattr(sessions, "maybe_build_llm_client", lambda: None)


def create_test_app_with_database(*, on_session=None, override_principal=True):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        with testing_session_local() as session:
            if on_session is not None:
                on_session(session)
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    if override_principal:
        async def override_get_current_principal(request: Request):
            payload = await request.json() if request.method == "POST" else {}
            actor_id = payload.get("actor_id") or request.query_params.get("actor_id")
            user_id = payload.get("user_id", actor_id or "user_1")
            if user_id == "admin_1":
                scopes = ("*",)
            elif user_id == "approver_1":
                scopes = (
                    "agent:invoke",
                    "agent:read",
                    "approval:decide",
                    "approval:read",
                    "mcp:mock",
                    "rag:retrieve",
                )
            else:
                scopes = (
                    "agent:invoke",
                    "agent:read",
                    "mcp:mock",
                    "rag:retrieve",
                )
            return Principal(
                tenant_id=payload.get(
                    "tenant_id",
                    request.query_params.get("tenant_id", "tenant_1"),
                ),
                user_id=user_id,
                scopes=scopes,
            )

        app.dependency_overrides[get_current_principal] = (
            override_get_current_principal
        )
    return app, testing_session_local


def _initial_state_from_graph_kwargs(kwargs):
    return create_initial_state(
        session_id=kwargs["session_id"],
        session_key=kwargs["session_key"],
        tenant_id=kwargs["tenant_id"],
        user_id=kwargs["user_id"],
        user_message=kwargs["user_message"],
        request_id=kwargs["request_id"],
        trace_id=kwargs["trace_id"],
        user_message_id=kwargs["user_message_id"],
        assistant_message_id=kwargs["assistant_message_id"],
        history=kwargs["history"],
        permission_scopes=kwargs.get("permission_scopes"),
        attachment_ids=kwargs.get("attachment_ids"),
    )


def _completed_graph_state(kwargs, *, content="ok"):
    state = _initial_state_from_graph_kwargs(kwargs)
    state["final_response"] = {
        "content": content,
        "trace_id": state["trace_id"],
        "cited_evidence_ids": [],
        "approval_status": "not_required",
    }
    return state


def test_post_message_persists_user_and_assistant_and_returns_stable_ids(monkeypatch):
    monkeypatch.setattr(
        sessions,
        "run_question_answer_graph",
        lambda **kwargs: _completed_graph_state(kwargs, content="持久化回答"),
    )
    app, testing_session_local = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        headers={"Idempotency-Key": "request-message-1"},
        json={"message": "分析银之杰"},
    )
    history = client.get("/api/sessions/s1/messages?limit=20")

    assert response.status_code == history.status_code == 200
    assert response.json()["request_id"] == "request-message-1"
    assert response.json()["message_id"].startswith("msg_")
    assert [item["role"] for item in history.json()["items"]] == [
        "user",
        "assistant",
    ]
    assert [item["content"] for item in history.json()["items"]] == [
        "分析银之杰",
        "持久化回答",
    ]
    with testing_session_local() as db:
        records = list(db.scalars(select(AgentMessage).order_by(AgentMessage.sequence_no)))
        session_record = db.scalars(select(AgentSession)).one()
    assert [record.status for record in records] == ["completed", "completed"]
    assert session_record.active_run_id is None


def test_post_message_replays_idempotency_key_without_running_graph_twice(monkeypatch):
    graph_calls = []

    def fake_graph(**kwargs):
        graph_calls.append(kwargs)
        return _completed_graph_state(kwargs, content="同一个回答")

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, _ = create_test_app_with_database()
    client = TestClient(app)
    headers = {"Idempotency-Key": "request-replay-1"}

    first = client.post("/api/sessions/s1/messages", headers=headers, json={"message": "问题"})
    second = client.post("/api/sessions/s1/messages", headers=headers, json={"message": "问题"})

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert len(graph_calls) == 1


def test_get_messages_returns_404_for_cross_tenant_session(monkeypatch):
    monkeypatch.setattr(
        sessions,
        "run_question_answer_graph",
        lambda **kwargs: _completed_graph_state(kwargs),
    )
    app, _ = create_test_app_with_database()
    client = TestClient(app)
    created = client.post("/api/sessions/s1/messages", json={"message": "问题"})

    response = client.get(
        "/api/sessions/s1/messages",
        params={"tenant_id": "tenant_2", "actor_id": "user_1"},
    )

    assert created.status_code == 200
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "session_not_found"


def test_post_message_passes_only_recent_completed_visible_history(monkeypatch):
    captured = {}
    monkeypatch.setattr(sessions, "CONVERSATION_HISTORY_LIMIT", 4)

    def fake_graph(**kwargs):
        captured.update(kwargs)
        return _completed_graph_state(kwargs)

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as db:
        service = ConversationService(db)
        for index in range(1, 4):
            handle = service.begin_turn(
                BeginTurn(
                    tenant_id="tenant_1",
                    user_id="user_1",
                    session_id="s1",
                    request_id=f"seed-{index}",
                    user_content=f"旧问题{index}",
                )
            )
            service.complete_turn(
                tenant_id="tenant_1",
                run_id=handle.run_id,
                content=f"旧回答{index}",
            )
            db.commit()
        session_record = db.scalars(select(AgentSession)).one()
        session_record.next_sequence_no += 4
        db.add_all(
            [
                AgentMessage(
                    message_id="msg-failed",
                    tenant_id="tenant_1",
                    user_id="user_1",
                    session_key=session_record.session_key,
                    run_id="failed-run",
                    request_id="failed-request",
                    role="assistant",
                    status="failed",
                    content="失败回答",
                    sequence_no=7,
                ),
                AgentMessage(
                    message_id="msg-pending",
                    tenant_id="tenant_1",
                    user_id="user_1",
                    session_key=session_record.session_key,
                    run_id="pending-run",
                    request_id="pending-request",
                    role="assistant",
                    status="pending",
                    content="等待回答",
                    sequence_no=8,
                ),
                AgentMessage(
                    message_id="msg-interrupted",
                    tenant_id="tenant_1",
                    user_id="user_1",
                    session_key=session_record.session_key,
                    run_id="interrupted-run",
                    request_id="interrupted-request",
                    role="assistant",
                    status="interrupted",
                    content="中断回答",
                    sequence_no=9,
                ),
                AgentMessage(
                    message_id="msg-deleted",
                    tenant_id="tenant_1",
                    user_id="user_1",
                    session_key=session_record.session_key,
                    run_id="deleted-run",
                    request_id="deleted-request",
                    role="assistant",
                    status="completed",
                    content="已删除回答",
                    sequence_no=10,
                    completed_at=datetime.now(UTC),
                    deleted_at=datetime.now(UTC),
                ),
            ]
        )
        db.commit()

    response = TestClient(app).post(
        "/api/sessions/s1/messages",
        headers={"Idempotency-Key": "current-request"},
        json={"message": "当前问题"},
    )

    assert response.status_code == 200
    assert [message.content for message in captured["history"]] == [
        "旧回答2",
        "旧问题3",
        "旧回答3",
        "当前问题",
    ]
    assert captured["history"][-1].message_id == captured["user_message_id"]
    assert all("checkpoint" not in message.content for message in captured["history"])


def test_post_message_releases_turn_transaction_before_graph(monkeypatch):
    observed = {}

    def record_session(db):
        observed["db"] = db

    def fake_graph(**kwargs):
        observed["in_transaction"] = observed["db"].in_transaction()
        return _completed_graph_state(kwargs)

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, _ = create_test_app_with_database(on_session=record_session)

    response = TestClient(app).post("/api/sessions/s1/messages", json={"message": "问题"})

    assert response.status_code == 200
    assert observed["in_transaction"] is False


def test_post_message_generates_safe_request_id_when_header_is_missing(monkeypatch):
    captured = {}

    def fake_graph(**kwargs):
        captured.update(kwargs)
        return _completed_graph_state(kwargs)

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, _ = create_test_app_with_database()

    response = TestClient(app).post("/api/sessions/s1/messages", json={"message": "问题"})

    assert response.status_code == 200
    request_id = response.json()["request_id"]
    assert request_id == captured["request_id"]
    assert re.fullmatch(r"req_[0-9a-f]{32}", request_id)


def test_get_messages_uses_sequence_cursor_and_validates_boundaries(monkeypatch):
    monkeypatch.setattr(
        sessions,
        "run_question_answer_graph",
        lambda **kwargs: _completed_graph_state(kwargs),
    )
    app, _ = create_test_app_with_database()
    client = TestClient(app)
    client.post(
        "/api/sessions/s1/messages",
        headers={"Idempotency-Key": "page-request-1"},
        json={"message": "第一页问题"},
    )
    client.post(
        "/api/sessions/s1/messages",
        headers={"Idempotency-Key": "page-request-2"},
        json={"message": "第二页问题"},
    )

    newest = client.get("/api/sessions/s1/messages?limit=2")
    older = client.get(
        "/api/sessions/s1/messages",
        params={"limit": 2, "before_sequence": newest.json()["items"][0]["sequence_no"]},
    )

    assert [item["sequence_no"] for item in newest.json()["items"]] == [3, 4]
    assert [item["sequence_no"] for item in older.json()["items"]] == [1, 2]
    assert client.get("/api/sessions/s1/messages?limit=0").status_code == 422
    assert client.get("/api/sessions/s1/messages?limit=201").status_code == 422
    assert client.get("/api/sessions/s1/messages?before_sequence=0").status_code == 422


def test_session_api_uses_dependency_principal_instead_of_body_identity(monkeypatch):
    captured = {}

    def fake_graph(**kwargs):
        captured.update(kwargs)
        state = _initial_state_from_graph_kwargs(kwargs)
        state["final_response"] = {
            "content": "ok",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, _ = create_test_app_with_database(override_principal=False)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        tenant_id="trusted-tenant",
        user_id="trusted-user",
        scopes=("agent:invoke", "rag:retrieve"),
    )
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "attacker-tenant",
            "user_id": "attacker-user",
            "message": "分析问题",
        },
    )

    assert response.status_code == 200
    assert captured["tenant_id"] == "trusted-tenant"
    assert captured["user_id"] == "trusted-user"


def test_session_api_passes_attachment_refs_and_resolver_into_graph(monkeypatch):
    captured = {}

    class AttachmentResolver:
        def resolve(self, asset_ids, *, principal, session_id):
            captured["resolved_asset_ids"] = asset_ids
            captured["resolved_tenant_id"] = principal.tenant_id
            captured["resolved_session_id"] = session_id
            return [
                {
                    "asset_id": "asset-1",
                    "media_type": "image/png",
                    "width": 32,
                    "height": 16,
                    "status": "ready",
                    "document_type": "chart",
                    "summary": "图表摘要",
                    "extracted_text": "",
                    "financial_metrics": [],
                    "chart_observations": [],
                    "entities": [],
                    "risk_flags": [],
                    "confidence": 0.9,
                    "truncated": False,
                }
            ]

    def fake_graph(**kwargs):
        captured["attachment_ids"] = kwargs["attachment_ids"]
        captured["attachment_resolver"] = kwargs["attachment_resolver"]
        state = _initial_state_from_graph_kwargs(kwargs)
        state["final_response"] = {
            "content": "ok",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, _ = create_test_app_with_database()
    app.dependency_overrides[sessions.get_attachment_context_resolver] = (
        lambda: AttachmentResolver()
    )
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={"message": "分析这张图", "attachments": ["asset-1"]},
    )

    assert response.status_code == 200
    assert "resolved_asset_ids" not in captured
    assert captured["attachment_ids"] == ["asset-1"]
    assert isinstance(captured["attachment_resolver"], AttachmentResolver)


def test_session_api_rejects_unauthenticated_request_before_graph(monkeypatch):
    graph_calls = []
    monkeypatch.setattr(
        sessions,
        "run_question_answer_graph",
        lambda **kwargs: graph_calls.append(kwargs),
    )
    app, _ = create_test_app_with_database(override_principal=False)
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "attacker-tenant",
            "user_id": "attacker-user",
            "message": "分析问题",
        },
    )

    assert response.status_code == 401
    assert graph_calls == []


def test_post_session_message_persists_run_and_trace_events(monkeypatch):
    monkeypatch.setattr(sessions.settings, "use_mock_rag", True)
    app, testing_session_local = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "分析一下银之杰",
        },
    )

    assert response.status_code == 200
    with testing_session_local() as session:
        saved_run = session.scalars(select(AgentRun)).one()
        saved_events = session.scalars(select(AgentTraceEvent)).all()

    assert saved_run.session_id == "s1"
    assert saved_run.status == "completed"
    assert saved_run.trace_id == response.json()["trace_id"]
    assert {event.node_name for event in saved_events} >= {"intent_router", "final_response"}


def test_post_session_message_records_short_term_session_event(monkeypatch):
    captured = {}

    class RecordingShortTermStore:
        def append_session_event(self, *, tenant_id, session_id, event, ttl_seconds=None):
            captured["tenant_id"] = tenant_id
            captured["session_id"] = session_id
            captured["event"] = event
            captured["ttl_seconds"] = ttl_seconds

    def fake_graph(**kwargs):
        state = _initial_state_from_graph_kwargs(kwargs)
        state["intent"] = {
            "intent": "knowledge_qa",
            "risk_level": "medium",
            "needs_clarification": False,
        }
        state["final_response"] = {
            "content": "ok",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    monkeypatch.setattr(
        sessions,
        "build_short_term_memory_store",
        lambda: RecordingShortTermStore(),
    )
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "分析一下银之杰",
        },
    )

    assert response.status_code == 200
    assert captured["tenant_id"] == "t1"
    assert captured["session_id"] == "s1"
    assert captured["event"]["intent"] == "knowledge_qa"
    assert captured["event"]["approval_status"] == "not_required"
    assert captured["event"]["trace_id"] == response.json()["trace_id"]


def test_post_session_message_ignores_short_term_memory_failure(monkeypatch):
    class FailingShortTermStore:
        def append_session_event(self, *, tenant_id, session_id, event, ttl_seconds=None):
            raise RuntimeError("redis unavailable")

    def fake_graph(**kwargs):
        state = _initial_state_from_graph_kwargs(kwargs)
        state["final_response"] = {
            "content": "ok",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    monkeypatch.setattr(
        sessions,
        "build_short_term_memory_store",
        lambda: FailingShortTermStore(),
    )
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "分析一下银之杰",
        },
    )

    assert response.status_code == 200
    assert response.json()["content"] == "ok"


def test_post_session_message_uses_configured_rag_mode(monkeypatch):
    captured = {}

    def fake_graph(**kwargs):
        captured["mock_evidence"] = kwargs["mock_evidence"]
        state = _initial_state_from_graph_kwargs(kwargs)
        state["intent"] = {
            "intent": "knowledge_qa",
            "risk_level": "medium",
            "needs_clarification": False,
                    "candidate_skill": "company_snapshot",
            "reason_codes": [],
            "next_node": "skill_agent",
        }
        state["final_response"] = {
            "content": "ok",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions.settings, "use_mock_rag", False, raising=False)
    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "分析一下银之杰",
        },
    )

    assert response.status_code == 200
    assert captured["mock_evidence"] is False


def test_post_session_message_uses_graph_checkpointer(monkeypatch):
    captured = {}
    checkpointer = object()

    def fake_graph(**kwargs):
        captured["checkpointer"] = kwargs.get("checkpointer")
        state = _initial_state_from_graph_kwargs(kwargs)
        state["final_response"] = {
            "content": "ok",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "build_graph_checkpointer", lambda: checkpointer, raising=False)
    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "分析一下银之杰",
        },
    )

    assert response.status_code == 200
    assert captured["checkpointer"] is checkpointer


def test_post_session_message_persists_approved_experience_memory(monkeypatch):
    def fake_graph(**kwargs):
        state = _initial_state_from_graph_kwargs(kwargs)
        state["memory_candidates"] = [
            MemoryCandidate(
                candidate_id="memcand_1",
                memory_type="user_preference",
                content="所有的文档用中文",
                confidence=0.9,
                approval_status="approved",
                sensitivity="low",
                source="user_explicit_preference",
            )
        ]
        state["approval_requests"] = [
            ApprovalRequest(
                approval_id="approval_1",
                run_id=state["request_id"],
                approval_type="memory_write",
                risk_level="low",
                payload={"candidate_id": "memcand_1"},
                status="approved",
            )
        ]
        state["final_response"] = {
            "content": "ok",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, testing_session_local = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "tenant_1",
            "user_id": "user_1",
            "message": "记住我的语言偏好，所有的文档用中文",
        },
    )

    assert response.status_code == 200
    with testing_session_local() as session:
        saved = session.scalars(select(ExperienceEntryRecord)).one()

    assert saved.source_candidate_id == "memcand_1"
    assert saved.content == "所有的文档用中文"


def test_post_session_message_recalls_experience_memory_for_response(monkeypatch):
    monkeypatch.setattr(sessions.settings, "use_mock_rag", True)
    monkeypatch.setattr(
        sessions, "maybe_build_llm_client", lambda: PreferenceResponseLlmClient()
    )
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ExperienceEntryRecord(
                entry_id="exp_1",
                tenant_id="tenant_1",
                user_id="user_1",
                memory_type="user_preference",
                content="所有回答用中文",
                source_candidate_id="memcand_existing",
                sensitivity="low",
                source="user_explicit_preference",
            )
        )
        session.commit()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "tenant_1",
            "user_id": "user_1",
            "message": "请继续使用我的语言偏好",
        },
    )

    assert response.status_code == 200
    assert response.json()["content"].startswith("按你的中文偏好回答：")


def test_approval_api_uses_dependency_principal_for_tenant_scope():
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        for tenant_id, approval_id in (
            ("trusted-tenant", "approval_trusted"),
            ("attacker-tenant", "approval_attacker"),
        ):
            session.add(
                ApprovalRecord(
                    approval_id=approval_id,
                    run_id=f"run_{tenant_id}",
                    session_id="session_1",
                    session_key=f"session_key_{tenant_id}",
                    tenant_id=tenant_id,
                    request_user_id="request-user",
                    approval_type="tool_call",
                    tool_name="mcp.mock.trade.submit",
                    tool_version="1.0.0",
                    arguments_hash=f"hash_{tenant_id}",
                    payload_json="{}",
                    checkpoint_thread_id=f"run_{tenant_id}",
                    status="pending",
                )
            )
        session.commit()
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        tenant_id="trusted-tenant",
        user_id="trusted-approver",
        scopes=("approval:read", "approval:decide"),
    )
    client = TestClient(app)

    response = client.get(
        "/api/approvals/pending",
        params={"tenant_id": "attacker-tenant", "actor_id": "attacker-user"},
    )

    assert response.status_code == 200
    assert [item["approval_id"] for item in response.json()["items"]] == [
        "approval_trusted"
    ]


def test_approval_endpoint_rejects_analyst_and_allows_mock_approver():
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="pending",
            )
        )
        session.commit()
    client = TestClient(app)

    denied = client.get(
        "/api/approvals/pending",
        params={"tenant_id": "tenant_1", "actor_id": "user_1"},
    )
    response = client.get(
        "/api/approvals/pending",
        params={"tenant_id": "tenant_1", "actor_id": "approver_1"},
    )

    assert denied.status_code == 403
    assert response.status_code == 200
    assert response.json()["items"][0]["approval_id"] == "approval_1"


def test_approval_api_redacts_sensitive_payload_defensively():
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_secret",
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.secret_write",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json=json.dumps(
                    {
                        "arguments": {
                            "clientSecret": "client-value",
                            "nested": {"refresh-token": "refresh-value"},
                            "visible": "safe-value",
                        }
                    }
                ),
                checkpoint_thread_id="run_1",
                status="pending",
            )
        )
        session.commit()

    response = TestClient(app).get(
        "/api/approvals/pending",
        params={"tenant_id": "tenant_1", "actor_id": "approver_1"},
    )

    assert response.status_code == 200
    arguments = response.json()["items"][0]["payload"]["arguments"]
    assert arguments == {
        "clientSecret": "***",
        "nested": {"refresh-token": "***"},
        "visible": "safe-value",
    }


def test_post_session_message_persists_pending_tool_approval(monkeypatch):
    def interrupted_graph(**kwargs):
        state = _initial_state_from_graph_kwargs(kwargs)
        state["intent"] = {
            "intent": "tool_action",
            "risk_level": "high",
            "needs_clarification": False,
        }
        state["__interrupt__"] = [
            Interrupt(
                value={
                    "approval_id": "approval_1",
                    "approval_type": "tool_call",
                        "run_id": state["request_id"],
                        "session_id": state["session_id"],
                        "session_key": state["session_key"],
                        "tenant_id": state["tenant_id"],
                    "request_user_id": state["user_id"],
                    "tool_name": "mcp.mock.trade.submit",
                    "tool_version": "1.0.0",
                    "arguments_hash": "hash_1",
                    "arguments": {"symbol": "600519"},
                    "purpose": "提交模拟交易",
                    "checkpoint_thread_id": state["request_id"],
                    "status": "pending",
                }
            )
        ]
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", interrupted_graph)
    app, testing_session_local = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "tenant_1",
            "user_id": "user_1",
            "message": "提交一笔模拟交易",
        },
    )

    assert response.status_code == 200
    assert response.json()["approval_status"] == "pending"
    assert response.json()["approval_ids"] == ["approval_1"]
    with testing_session_local() as session:
        approval = session.get(ApprovalRecord, "approval_1")
        run = session.scalars(select(AgentRun)).one()
    assert approval.arguments_hash == "hash_1"
    assert approval.run_id == run.run_id
    assert approval.session_key == run.session_key
    assert run.status == "interrupted"


def test_approver_decision_preserves_checkpoint_request_user_scopes(monkeypatch):
    captured = {}

    def fake_resume(**kwargs):
        captured.update(kwargs)
        state = create_initial_state(
            session_id="session_1",
            session_key="session_key_1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="提交一笔模拟交易",
            permission_scopes=["mcp:mock", "rag:retrieve"],
        )
        state["request_id"] = "run_1"
        state["trace_id"] = "trace_1"
        state["final_response"] = {
            "content": "模拟交易已提交。",
            "trace_id": "trace_1",
            "cited_evidence_ids": [],
            "approval_status": "approved",
        }
        return state

    monkeypatch.setattr(approvals, "resume_question_answer_graph", fake_resume)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="pending",
            )
        )
        session.commit()
    client = TestClient(app)

    response = client.post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 200
    assert captured["checkpoint_thread_id"] == "run_1"
    assert captured["approval_decision"] == {
        "approval_id": "approval_1",
        "arguments_hash": "hash_1",
        "decision": "approved",
    }
    assert captured["permission_scopes"] is None
    assert response.json()["run_status"] == "completed"
    with testing_session_local() as session:
        approval = session.get(ApprovalRecord, "approval_1")
        run = session.get(AgentRun, "run_1")
    assert approval.status == "approved"
    assert approval.decided_by == "approver_1"
    assert run.status == "completed"


def test_approval_resume_does_not_hold_request_database_transaction(monkeypatch):
    request_session = None

    def capture_session(session):
        nonlocal request_session
        request_session = session

    def fake_resume(**kwargs):
        assert request_session is not None
        assert request_session.in_transaction() is False
        state = create_initial_state(
            session_id="session_1",
            session_key="session_key_1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="提交一笔模拟交易",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = "run_1"
        state["trace_id"] = "trace_1"
        state["final_response"] = {
            "content": "模拟交易已提交。",
            "trace_id": "trace_1",
            "cited_evidence_ids": [],
            "approval_status": "approved",
        }
        return state

    monkeypatch.setattr(approvals, "resume_question_answer_graph", fake_resume)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, testing_session_local = create_test_app_with_database(
        on_session=capture_session,
    )
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="pending",
            )
        )
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="interrupted",
            )
        )
        session.commit()
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 200


def test_repeated_same_approval_decision_does_not_resume_checkpoint_again(monkeypatch):
    resume_calls = []

    def unexpected_resume(**kwargs):
        resume_calls.append(kwargs)
        raise AssertionError("相同审批决定不应重复恢复 checkpoint")

    monkeypatch.setattr(approvals, "resume_question_answer_graph", unexpected_resume)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="approved",
                decided_by="approver_1",
            )
        )
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="completed",
            )
        )
        session.commit()
    client = TestClient(app)

    response = client.post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 200
    assert response.json()["approval"]["status"] == "approved"
    assert response.json()["run_status"] == "completed"
    assert response.json()["final_response"] is None
    assert resume_calls == []


@pytest.mark.parametrize(
    ("resume_error", "expected_code"),
    [
        (
            AgentGraphExecutionError(
                node_name="approval_gate",
                message="工具参数已变化",
                code="approval_payload_mismatch",
                http_status=409,
            ),
            "approval_payload_mismatch",
        ),
        (RuntimeError("checkpoint missing"), "approval_resume_failed"),
    ],
)
def test_approval_resume_failure_is_409_and_remains_retryable(
    monkeypatch,
    resume_error,
    expected_code,
):
    def failing_resume(**_):
        raise resume_error

    monkeypatch.setattr(approvals, "resume_question_answer_graph", failing_resume)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="pending",
            )
        )
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="interrupted",
            )
        )
        session.commit()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == expected_code
    with testing_session_local() as session:
        approval = session.get(ApprovalRecord, "approval_1")
        run = session.get(AgentRun, "run_1")
    assert approval.status == "resume_failed"
    assert approval.decision == "approved"
    assert approval.resume_attempts == 1
    assert approval.last_resume_error
    assert run.status == "interrupted"


def test_approval_resume_failed_can_retry_same_decision(monkeypatch):
    resume_calls = []

    def successful_resume(**kwargs):
        resume_calls.append(kwargs)
        state = create_initial_state(
            session_id="session_1",
            session_key="session_key_1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="提交一笔模拟交易",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = "run_1"
        state["trace_id"] = "trace_1"
        state["final_response"] = {
            "content": "模拟交易已提交。",
            "trace_id": "trace_1",
            "cited_evidence_ids": [],
            "approval_status": "approved",
        }
        return state

    monkeypatch.setattr(approvals, "resume_question_answer_graph", successful_resume)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="resume_failed",
                decision="approved",
                resume_attempts=1,
                last_resume_error="checkpoint missing",
                decided_by="approver_1",
            )
        )
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="interrupted",
            )
        )
        session.commit()
    client = TestClient(app)

    response = client.post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 200
    assert len(resume_calls) == 1
    with testing_session_local() as session:
        approval = session.get(ApprovalRecord, "approval_1")
    assert approval.status == "approved"
    assert approval.resume_attempts == 2
    assert approval.last_resume_error == "checkpoint missing"
    assert approval.resume_claimed_by == "approver_1"


def test_approval_resume_in_progress_request_does_not_call_resume(monkeypatch):
    resume_calls = []
    monkeypatch.setattr(
        approvals,
        "resume_question_answer_graph",
        lambda **kwargs: resume_calls.append(kwargs),
    )
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="resuming",
                decision="approved",
                resume_claim_token="owned-by-another-request",
                resume_attempts=1,
                decided_by="approver_1",
            )
        )
        session.commit()
    client = TestClient(app)

    response = client.post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approval_resume_in_progress"
    assert resume_calls == []


def test_approval_resume_finalization_failure_is_409_and_retryable(monkeypatch):
    def successful_resume(**kwargs):
        state = create_initial_state(
            session_id="session_1",
            session_key="session_key_1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="提交一笔模拟交易",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = "run_1"
        state["trace_id"] = "trace_1"
        state["final_response"] = {
            "content": "模拟交易已提交。",
            "trace_id": "trace_1",
            "cited_evidence_ids": [],
            "approval_status": "approved",
        }
        return state

    monkeypatch.setattr(approvals, "resume_question_answer_graph", successful_resume)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    monkeypatch.setattr(
        approvals,
        "persist_agent_run",
        lambda **_: (_ for _ in ()).throw(RuntimeError("run persistence failed")),
    )
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="pending",
            )
        )
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="interrupted",
            )
        )
        session.commit()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/approvals/approval_1/decision",
        json={
            "tenant_id": "tenant_1",
            "actor_id": "approver_1",
            "decision": "approved",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approval_resume_failed"
    with testing_session_local() as session:
        approval = session.get(ApprovalRecord, "approval_1")
        run = session.get(AgentRun, "run_1")
    assert approval.status == "resume_failed"
    assert "run persistence failed" in approval.last_resume_error
    assert run.status == "interrupted"


def test_approval_retry_persists_rolled_back_trace_once(monkeypatch):
    trace_store = InMemoryTraceStore()
    resume_calls = 0

    def resume_with_trace_only_once(**kwargs):
        nonlocal resume_calls
        resume_calls += 1
        state = create_initial_state(
            session_id="session_1",
            session_key="session_key_1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="提交一笔模拟交易",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = "run_1"
        state["trace_id"] = "trace_1"
        state["final_response"] = {
            "content": "模拟交易已提交。",
            "trace_id": "trace_1",
            "cited_evidence_ids": [],
            "approval_status": "approved",
        }
        if resume_calls == 1:
            trace_store.record_event(
                TraceEvent(
                    trace_id="trace_1",
                    node_name="tool_agent",
                    status="success",
                    latency_ms=2,
                )
            )
        return state

    original_complete_resume = approvals.ApprovalRepository.complete_resume
    complete_calls = 0

    def fail_first_finalization(repository, **kwargs):
        nonlocal complete_calls
        complete_calls += 1
        if complete_calls == 1:
            raise RuntimeError("approval finalization failed")
        return original_complete_resume(repository, **kwargs)

    monkeypatch.setattr(
        approvals,
        "resume_question_answer_graph",
        resume_with_trace_only_once,
    )
    monkeypatch.setattr(approvals, "get_trace_store", lambda: trace_store)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    monkeypatch.setattr(
        approvals.ApprovalRepository,
        "complete_resume",
        fail_first_finalization,
    )
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_1",
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                request_user_id="user_1",
                approval_type="tool_call",
                tool_name="mcp.mock.trade.submit",
                tool_version="1.0.0",
                arguments_hash="hash_1",
                payload_json="{}",
                checkpoint_thread_id="run_1",
                status="pending",
            )
        )
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="interrupted",
            )
        )
        session.commit()
    client = TestClient(app, raise_server_exceptions=False)
    request = {
        "tenant_id": "tenant_1",
        "actor_id": "approver_1",
        "decision": "approved",
    }

    first_response = client.post(
        "/api/approvals/approval_1/decision",
        json=request,
    )
    second_response = client.post(
        "/api/approvals/approval_1/decision",
        json=request,
    )

    assert first_response.status_code == 409
    assert second_response.status_code == 200
    assert resume_calls == 2
    with testing_session_local() as session:
        saved_run = session.get(AgentRun, "run_1")
        saved_events = session.scalars(select(AgentTraceEvent)).all()
    assert saved_run.status == "completed"
    assert [event.node_name for event in saved_events] == ["tool_agent"]


def test_post_session_message_returns_500_for_graph_execution_error(monkeypatch):
    def failing_graph(**_):
        raise AgentGraphExecutionError(node_name="retriever_agent", message="rag unavailable")

    monkeypatch.setattr(sessions, "run_question_answer_graph", failing_graph)
    app, testing_session_local = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        headers={"X-Tenant-ID": "t1", "X-User-ID": "u1"},
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "分析一下银之杰",
        },
    )

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "agent_graph_execution_failed",
            "node": "retriever_agent",
            "message": "rag unavailable",
        }
    }
    with testing_session_local() as db:
        saved_run = db.scalars(select(AgentRun)).one()
        saved_session = db.scalars(select(AgentSession)).one()
        saved_assistant = db.scalar(
            select(AgentMessage).where(AgentMessage.role == "assistant")
        )
    assert saved_run.status == "failed"
    assert saved_session.active_run_id is None
    assert saved_assistant.status == "failed"
    assert saved_assistant.metadata_json == {"error_code": "agent_execution_failed"}


def test_post_session_message_uses_graph_error_http_status(monkeypatch):
    def failing_graph(**_):
        raise AgentGraphExecutionError(
            node_name="retriever_agent",
            message="No Chroma index found.",
            code="rag_index_unavailable",
            http_status=503,
        )

    monkeypatch.setattr(sessions, "run_question_answer_graph", failing_graph)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        headers={"X-Tenant-ID": "t1", "X-User-ID": "u1"},
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "分析一下银之杰",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "rag_index_unavailable"


def test_execution_error_response_is_completed_without_approval_record(monkeypatch):
    def execution_error_graph(**kwargs):
        state = _initial_state_from_graph_kwargs(kwargs)
        state["draft_answer"] = {
            "content": "检索服务暂不可用。",
            "claims": [],
            "cited_evidence_ids": [],
            "response_type": "execution_error_response",
            "business_error_code": "rag_index_unavailable",
        }
        state["verification"] = {
            "verdict": "execution_error",
            "required_action": "respond_with_execution_error",
        }
        state["approval_status"] = ApprovalStatus(
            required=False,
            status="not_required",
        )
        state["final_response"] = {
            "content": "检索服务暂不可用。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
            "response_type": "execution_error_response",
            "business_error_code": "rag_index_unavailable",
        }
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", execution_error_graph)
    app, testing_session_local = create_test_app_with_database()
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "查询投研资料",
        },
    )

    assert response.status_code == 200
    assert response.json()["response_type"] == "execution_error_response"
    assert response.json()["business_error_code"] == "rag_index_unavailable"
    assert response.json()["approval_status"] == "not_required"
    with testing_session_local() as session:
        assert session.scalars(select(ApprovalRecord)).all() == []


def test_post_session_message_stream_returns_answer_deltas_and_final(monkeypatch):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["intent"] = {
            "intent": "tool_action",
            "risk_level": "low",
            "needs_clarification": False,
        }
        state["final_response"] = {
            "content": "计算完成：总和是 6。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        kwargs["trace_store"].record_event(
            TraceEvent(
                trace_id=state["trace_id"],
                node_name="intent_router",
                status="success",
                latency_ms=2,
                metadata={"summary": "识别为工具计算请求"},
            )
        )
        yield state

    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "1+2+3=?",
        },
    ) as response:
        body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert "event: status" in body
    assert "event: agent_event" in body
    assert "intent_router" in body
    assert "event: answer_delta" in body
    assert "计算完成：总和是 6。" in body
    assert "event: final" in body
    assert "event: done" in body


def test_product_progress_stage_tracks_equity_research_pipeline():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下岩山科技",
    )
    market_request = ToolCallRequest(
        call_id="market_1",
        tool_name="mcp.local.stock.market_snapshot",
        tool_version="1",
        arguments={"symbol": "002195.SZ"},
        purpose="获取行情",
    )
    rag_request = ToolCallRequest(
        call_id="rag_1",
        tool_name="rag_search",
        tool_version="1",
        arguments={"query": "岩山科技"},
        purpose="检索补充资料",
    )
    state["plan"] = {
        "execution_mode": "tool_execution",
        "requested_tools": [market_request, rag_request],
        "response_policy": {"response_type": "equity_research_report"},
    }

    assert sessions._product_progress_stage(state) == "collecting_market_data"

    state["tool_calls"] = [
        ToolCallRecord(
            call_id="market_1",
            request=market_request,
            result=ToolCallResult(
                tool_call_id="market_1",
                tool_name=market_request.tool_name,
                status="success",
                result={"data": {"price": 6.83}},
                trace_id=state["trace_id"],
            ),
        )
    ]
    state["ready_tool_call"] = ReadyToolCall(
        call_id="rag_1",
        step_index=1,
        request_fingerprint="fingerprint",
        tool_name="rag_search",
    )
    assert sessions._product_progress_stage(state) == "retrieving_knowledge"

    state["ready_tool_call"] = None
    assert sessions._product_progress_stage(state) == "analyzing_evidence"

    state["analysis"] = {"findings": [], "missing_information": []}
    assert sessions._product_progress_stage(state) == "composing_answer"

    state["draft_answer"] = {"content": "完成"}
    assert sessions._product_progress_stage(state) is None


def test_post_session_message_stream_emits_product_progress_without_trace_details(
    monkeypatch,
):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["plan"] = {
            "execution_mode": "tool_execution",
            "requested_tools": [
                ToolCallRequest(
                    tool_name="mcp.local.stock.market_snapshot",
                    arguments={"symbol": "002195.SZ"},
                    purpose="获取行情",
                )
            ],
            "response_policy": {"response_type": "equity_research_report"},
        }
        yield state
        state["final_response"] = {
            "content": "岩山科技投研结论。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()

    body = TestClient(app).post(
        "/api/sessions/s1/messages/stream",
        json={"message": "分析一下岩山科技"},
    ).text

    progress_events = "\n".join(
        block
        for block in body.split("\n\n")
        if block.startswith("event: progress")
    )
    assert progress_events.count('"stage": "planning"') == 1
    assert progress_events.count('"stage": "collecting_market_data"') == 1
    assert "node_name" not in progress_events
    assert "arguments" not in progress_events


def test_post_session_message_stream_emits_llm_thinking_before_final(monkeypatch):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["intent"] = {
            "intent": "task_workflow",
            "domain": "a_share_equity_research",
            "sub_intent": "company_snapshot",
            "risk_level": "medium",
            "needs_clarification": False,
            "candidate_skill": "company_snapshot",
        }
        state["plan"] = {
            "objective": "分析银之杰公司概况",
            "execution_mode": "skill_execution",
            "selected_skill": {"skill_name": "company_snapshot"},
            "execution_steps": ["识别公司实体", "获取市场数据", "生成投研摘要"],
            "retrieval": {"required": True, "strategy": "rag_search"},
            "tool_policy": {"allowed_tools": ["mcp.*", "rag_search"]},
            "response_policy": {},
            "risk_controls": {},
        }
        state["analysis"] = {
            "findings": [],
            "missing_information": ["公告检索权限不足"],
            "confidence": 0.6,
            "data_coverage": [
                {"capability": "stock.company_profile", "status": "complete"},
                {"capability": "stock.announcement_search", "status": "failed"},
            ],
        }
        state["final_response"] = {
            "content": "银之杰公司概览。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    monkeypatch.setattr(sessions, "maybe_build_llm_client", StreamingNarrativeLlmClient)
    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={"message": "分析一下银之杰"},
    ) as response:
        body = response.read().decode("utf-8")

    assert response.status_code == 200
    thinking_index = body.index("event: thinking_delta")
    answer_index = body.index("event: answer_delta")
    final_index = body.index("event: final")
    assert thinking_index < answer_index < final_index
    assert "我先识别你的投研目标" in body
    assert "再检查可用市场数据" in body
    assert "银之杰公司概览。" in body


def test_post_session_message_stream_emits_thinking_before_graph_completed(
    monkeypatch,
):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["intent"] = {
            "intent": "task_workflow",
            "domain": "a_share_equity_research",
            "sub_intent": "company_snapshot",
            "risk_level": "medium",
            "needs_clarification": False,
            "candidate_skill": "company_snapshot",
        }
        state["plan"] = {
            "objective": "分析银之杰公司概况",
            "execution_mode": "skill_execution",
            "selected_skill": {"skill_name": "company_snapshot"},
            "execution_steps": ["识别公司实体", "获取市场数据", "生成投研摘要"],
            "retrieval": {"required": True, "strategy": "rag_search"},
            "tool_policy": {"allowed_tools": ["mcp.*", "rag_search"]},
            "response_policy": {},
            "risk_controls": {},
        }
        kwargs["trace_store"].record_event(
            TraceEvent(
                trace_id=state["trace_id"],
                node_name="planner_agent",
                status="success",
                latency_ms=2,
                metadata={"summary": "选择 skill_execution"},
            )
        )
        yield state
        state["final_response"] = {
            "content": "银之杰公司概览。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    monkeypatch.setattr(sessions, "maybe_build_llm_client", StreamingNarrativeLlmClient)
    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={"message": "分析一下银之杰"},
    ) as response:
        body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert body.index("event: thinking_delta") < body.index('"stage": "graph_completed"')
    assert body.index("event: thinking_delta") < body.index("event: answer_delta")


def test_post_session_message_stream_emits_multiple_public_thinking_stages(
    monkeypatch,
):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["intent"] = {
            "intent": "task_workflow",
            "domain": "a_share_equity_research",
            "sub_intent": "company_snapshot",
            "risk_level": "medium",
            "needs_clarification": False,
            "candidate_skill": "company_snapshot",
        }
        state["plan"] = {
            "objective": "分析长鑫存储",
            "execution_mode": "tool_execution",
            "analysis_dimensions": ["business", "market", "financial", "risk"],
            "requested_tools": [
                ToolCallRequest(
                    tool_name="mcp.local.stock.resolve_company",
                    arguments={"query": "长鑫存储"},
                    purpose="解析公司",
                )
            ],
            "retrieval": {"required": True, "strategy": "rag_search"},
            "response_policy": {"response_type": "equity_research_report"},
        }
        yield state
        state["plan"]["tool_repair"] = {
            "status": "recovered",
            "strategy": "web_search_entity_discovery_and_market_revalidation",
            "failed_query": "长鑫存储",
            "recovered_query": "长鑫科技",
            "source_urls": ["https://www.sse.com.cn/disclosure/example"],
            "observation_error_code": "required_data_missing",
        }
        state["tool_calls"] = [
            ToolCallRecord(
                request=state["plan"]["requested_tools"][0],
                result=ToolCallResult(
                    tool_call_id="tool_1",
                    tool_name="mcp.local.stock.resolve_company",
                    status="success",
                    result={"symbol": "688825.SH", "name": "长鑫科技"},
                    trace_id=state["trace_id"],
                ),
            )
        ]
        yield state
        state["analysis"] = {
            "findings": ["已确认长鑫科技。"],
            "missing_information": ["公告权限不足"],
            "confidence": 0.7,
            "data_coverage": [
                {"capability": "stock.resolve_company", "status": "complete"}
            ],
        }
        yield state
        state["final_response"] = {
            "content": "长鑫科技投研结论。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    monkeypatch.setattr(sessions, "maybe_build_llm_client", StreamingNarrativeLlmClient)
    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()

    body = TestClient(app).post(
        "/api/sessions/s1/messages/stream",
        json={"message": "分析一下长鑫存储"},
    ).text

    thinking_events = [
        block for block in body.split("\n\n") if block.startswith("event: thinking_delta")
    ]
    assert len(thinking_events) >= 3
    thinking_text = "\n".join(thinking_events)
    assert "我先识别你的投研目标" in thinking_text
    assert "node_name" not in thinking_text
    assert "tool_calls" not in thinking_text
    assert body.index("event: thinking_delta") < body.index("event: answer_delta")


def test_post_session_message_stream_drops_incomplete_thinking_sentence(
    monkeypatch,
):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["intent"] = {
            "intent": "task_workflow",
            "domain": "a_share_equity_research",
            "sub_intent": "company_snapshot",
            "risk_level": "medium",
            "needs_clarification": False,
            "candidate_skill": "company_snapshot",
        }
        state["plan"] = {
            "objective": "分析银之杰公司概况",
            "execution_mode": "skill_execution",
            "selected_skill": {"skill_name": "company_snapshot"},
            "execution_steps": ["识别公司实体", "获取市场数据", "生成投研摘要"],
            "retrieval": {"required": True, "strategy": "rag_search"},
            "tool_policy": {"allowed_tools": ["mcp.*", "rag_search"]},
            "response_policy": {},
            "risk_controls": {},
        }
        yield state
        state["final_response"] = {
            "content": "银之杰公司概览。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    monkeypatch.setattr(
        sessions,
        "maybe_build_llm_client",
        IncompleteStreamingNarrativeLlmClient,
    )
    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={"message": "分析一下银之杰"},
    ) as response:
        body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert "我会先拆解" in body
    assert "我会检查公司资料" in body
    assert "市场系统性风险可能带来的" not in body


def test_post_session_message_stream_emits_fallback_thinking_without_streaming_llm(
    monkeypatch,
):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["intent"] = {
            "intent": "task_workflow",
            "domain": "a_share_equity_research",
            "sub_intent": "company_snapshot",
            "risk_level": "medium",
            "needs_clarification": False,
        }
        state["final_response"] = {
            "content": "银之杰公司概览。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    monkeypatch.setattr(sessions, "maybe_build_llm_client", lambda: None)
    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={"message": "分析一下银之杰"},
    ) as response:
        body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert "event: thinking_delta" in body
    assert "我会先确认公司实体" in body


def test_post_session_message_stream_falls_back_when_thinking_stream_fails(
    monkeypatch,
):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["intent"] = {
            "intent": "task_workflow",
            "domain": "a_share_equity_research",
            "sub_intent": "company_snapshot",
            "risk_level": "medium",
            "needs_clarification": False,
        }
        state["final_response"] = {
            "content": "最终答案仍然返回。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    monkeypatch.setattr(
        sessions,
        "maybe_build_llm_client",
        FailingStreamingNarrativeLlmClient,
    )
    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={"message": "分析一下银之杰"},
    ) as response:
        body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert "event: thinking_delta" in body
    assert "我会先确认公司实体" in body
    assert "最终答案仍然返回。" in body
    assert "event: final" in body


def test_fallback_thinking_uses_business_dimensions_instead_of_internal_steps():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下岩山科技",
    )
    state["intent"] = {"domain": "a_share_equity_research"}
    state["plan"] = {
        "execution_mode": "tool_execution",
        "execution_steps": [
            "understand_equity_research_intent",
            "execute_mcp_tools",
        ],
        "analysis_dimensions": ["business", "financial", "risk"],
    }

    content = "".join(sessions._fallback_thinking_chunks(state))

    assert "业务、财务和风险" in content
    assert "understand_equity_research_intent" not in content
    assert "execute_mcp_tools" not in content


def test_post_session_message_stream_returns_error_for_missing_llm_configuration(
    monkeypatch,
):
    def missing_llm_client():
        raise LlmConfigurationError("DeepSeek api key is required.")

    monkeypatch.setattr(sessions, "maybe_build_llm_client", missing_llm_client)
    app, _ = create_test_app_with_database()
    client = TestClient(app, raise_server_exceptions=False)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={"message": "分析一下银之杰"},
    ) as response:
        body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert "event: error" in body
    assert "llm_configuration_error" in body
    assert "BS_AGENT_DEEPSEEK_API_KEY" in body
    assert 'event: done\ndata: {"ok": false}' in body


def test_post_session_message_stream_returns_error_when_persistence_fails(
    monkeypatch,
):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["final_response"] = {
            "content": "这条回答不会在持久化失败时发送。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    def failing_persist(**_):
        raise RuntimeError("schema drift")

    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    monkeypatch.setattr(sessions, "_persist_agent_message_state", failing_persist)
    app, _ = create_test_app_with_database()
    client = TestClient(app, raise_server_exceptions=False)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={"message": "分析一下银之杰"},
    ) as response:
        body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert "event: error" in body
    assert "agent_stream_internal_error" in body
    assert 'event: done\ndata: {"ok": false}' in body
    assert "event: final" not in body


def test_post_session_message_stream_returns_a_share_research_answer(monkeypatch):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["final_response"] = {
            "content": (
                "公司概览\n"
                "贵州茅台，行业：食品饮料。\n\n"
                "免责声明\n"
                "以上内容仅用于投研辅助分析，不构成投资建议、买卖建议或收益承诺。"
            ),
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={
            "tenant_id": "tenant_1",
            "user_id": "user_1",
            "message": "分析一下贵州茅台",
        },
    ) as response:
        body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert "event: trace_events" in body
    assert "公司概览" in body
    assert "不构成投资建议" in body


def test_stream_emits_attachment_loading_and_analysis_loaded_stages(monkeypatch):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
            attachment_ids=kwargs["attachment_ids"],
        )
        state["attachment_refs"] = [
            {
                "asset_id": "asset-1",
                "analysis_id": "analysis-1",
                "status": "ready",
            }
        ]
        state["image_analyses"] = [
            {
                "asset_id": "asset-1",
                "analysis_id": "analysis-1",
                "media_type": "image/png",
                "width": 100,
                "height": 80,
                "status": "ready",
                "document_type": "chart",
                "summary": "趋势向上。",
                "extracted_text": "",
                "financial_metrics": [],
                "chart_observations": [],
                "entities": [],
                "risk_flags": [],
                "confidence": 0.9,
                "truncated": False,
            }
        ]
        state["final_response"] = {
            "content": "图表显示趋势向上。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={"message": "分析图片", "attachments": ["asset-1"]},
    ) as response:
        body = response.read().decode("utf-8")

    assert '"stage": "attachment_loading"' in body
    assert '"stage": "image_analysis_loaded"' in body
    assert '"attachment_count": 1' in body
