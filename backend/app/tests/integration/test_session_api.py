import asyncio
import inspect
import json
import re
from types import SimpleNamespace
from datetime import UTC, datetime

from fastapi import Request
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
import pytest
from langgraph.types import Interrupt
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import approvals, sessions
from app.conversations.service import BeginTurn, ConversationService
from app.graph.state import build_checkpoint_thread_id, create_initial_state
from app.graph import runtime
from app.graph.runtime import AgentGraphExecutionError
from app.harness.trace_buffer import TraceBuffer
from app.harness.tracing import TraceEvent
from app.intent.catalog import BusinessIntentCatalog
from app.intent.runtime import BusinessIntentCatalogUnavailableError
from app.llm.deepseek import LlmConfigurationError
from app.llm.schemas import LlmCompletionResult
from app.main import create_app
from app.memory.dependencies import get_memory_store
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
    MemoryCandidateRecord,
)
from app.tools.registry import ToolRegistry


TEST_MEMORY_STORE = object()
EMPTY_BUSINESS_INTENT_CATALOG = BusinessIntentCatalog([])


class _TestBusinessIntentCatalogRuntime:
    refresh_seconds = 3600

    def __init__(self, catalog: BusinessIntentCatalog):
        self.catalog = catalog
        self.snapshot_calls = 0
        self.refresh_calls = 0

    def open(self):
        return SimpleNamespace(catalog=self.catalog)

    def snapshot(self):
        self.snapshot_calls += 1
        return SimpleNamespace(catalog=self.catalog)

    def refresh(self):
        self.refresh_calls += 1
        return SimpleNamespace(catalog=self.catalog)

    def check_health(self):
        return None

    def close(self):
        return None


def test_api_modules_and_repository_expose_only_native_store_contract():
    from app.storage import repositories

    runtime_sources = "\n".join(
        inspect.getsource(module) for module in (sessions, approvals, repositories)
    )

    for removed_symbol in (
        "ExperienceStore",
        "SqlAlchemyExperienceStore",
        "ExperienceMemoryRetriever",
        "memory_retriever",
        "_ExperienceSnapshotReader",
        "experience_entries",
    ):
        assert removed_symbol not in runtime_sources
    assert "experience_store" not in inspect.signature(
        repositories.persist_agent_run
    ).parameters
    assert tuple(inspect.signature(sessions._ExecutionSnapshot).parameters) == (
        "history",
        "attachment_resolver",
    )
    assert "store=memory_store" in inspect.getsource(approvals.decide_approval)


@pytest.fixture(autouse=True)
def isolate_session_api_tests_from_external_mcp(monkeypatch):
    monkeypatch.setattr(runtime, "get_default_tool_registry", ToolRegistry)
    monkeypatch.setattr(sessions, "build_graph_checkpointer", InMemorySaver)
    monkeypatch.setattr(approvals, "build_graph_checkpointer", InMemorySaver)


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


def create_test_app_with_database(
    *,
    on_session=None,
    override_principal=True,
    business_intent_catalog_runtime=None,
):
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

    app = create_app(
        business_intent_catalog_runtime=(
            business_intent_catalog_runtime
            or _TestBusinessIntentCatalogRuntime(EMPTY_BUSINESS_INTENT_CATALOG)
        )
    )
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_memory_store] = lambda: TEST_MEMORY_STORE
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


def add_approval_resume_fixture(
    db,
    *,
    approval_id="approval_1",
    run_id="run_1",
    request_id="checkpoint_request_1",
    checkpoint_thread_id=None,
    payload_overrides=None,
    status="pending",
    decision=None,
    resume_claim_token=None,
    resume_claimed_at=None,
    resume_attempts=0,
    last_resume_error=None,
    decided_by=None,
):
    expected_thread_id = build_checkpoint_thread_id(
        tenant_id="tenant_1",
        session_key="session_key_1",
        request_id=request_id,
    )
    selected_thread_id = checkpoint_thread_id or expected_thread_id
    projection = {
        "subject_name": "mcp.mock.trade.submit",
        "risk_level": "high",
        "action_digest": "digest_1",
        "checkpoint_id": "checkpoint_1",
        "interrupt_id": "interrupt_1",
    }
    projection.update(payload_overrides or {})
    db.add_all(
        [
            ApprovalRecord(
                approval_id=approval_id,
                run_id=run_id,
                tenant_id="tenant_1",
                approval_type="tool_call",
                subject_name=projection["subject_name"],
                risk_level=projection["risk_level"],
                action_digest=projection["action_digest"],
                checkpoint_thread_id=selected_thread_id,
                checkpoint_namespace="",
                checkpoint_id=projection["checkpoint_id"],
                interrupt_id=projection["interrupt_id"],
                requested_by="user_1",
                status=status,
                decision=decision,
                resume_claim_token=resume_claim_token,
                resume_claimed_at=resume_claimed_at,
                resume_attempts=resume_attempts,
                last_resume_error=last_resume_error,
                decided_by=decided_by,
            ),
            AgentRun(
                run_id=run_id,
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="interrupted",
            ),
            AgentSession(
                session_key="session_key_1",
                session_id="session_1",
                tenant_id="tenant_1",
                user_id="user_1",
                status="active",
                active_run_id=run_id,
                next_sequence_no=3,
            ),
            AgentMessage(
                message_id=f"message_{run_id}",
                tenant_id="tenant_1",
                user_id="user_1",
                session_key="session_key_1",
                run_id=run_id,
                request_id=request_id,
                role="user",
                status="completed",
                content="提交一笔模拟交易",
                sequence_no=1,
                completed_at=datetime.now(UTC),
            ),
            AgentMessage(
                message_id=f"assistant_{run_id}",
                tenant_id="tenant_1",
                user_id="user_1",
                session_key="session_key_1",
                run_id=run_id,
                request_id=request_id,
                role="assistant",
                status="interrupted",
                content="",
                sequence_no=2,
            ),
        ]
    )
    return expected_thread_id


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
    captured = {}

    def fake_graph(**kwargs):
        captured.update(kwargs)
        return _completed_graph_state(kwargs, content="持久化回答")

    monkeypatch.setattr(
        sessions,
        "run_question_answer_graph",
        fake_graph,
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
    assert captured["store"] is TEST_MEMORY_STORE
    assert "memory_retriever" not in captured


def test_post_message_uses_one_run_scoped_intent_catalog_snapshot(monkeypatch):
    captured = {}
    intent_runtime = _TestBusinessIntentCatalogRuntime(EMPTY_BUSINESS_INTENT_CATALOG)

    def fake_graph(**kwargs):
        captured.update(kwargs)
        return _completed_graph_state(kwargs)

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, _ = create_test_app_with_database(
        business_intent_catalog_runtime=intent_runtime
    )

    response = TestClient(app).post(
        "/api/sessions/s1/messages",
        json={"message": "分析银之杰"},
    )

    assert response.status_code == 200
    assert intent_runtime.snapshot_calls == 1
    assert intent_runtime.refresh_calls == 0
    assert captured["business_intent_catalog"] is intent_runtime.catalog


def test_post_message_returns_safe_503_when_intent_catalog_is_unavailable(monkeypatch):
    graph_calls = []

    class UnavailableIntentRuntime(_TestBusinessIntentCatalogRuntime):
        def snapshot(self):
            self.snapshot_calls += 1
            raise BusinessIntentCatalogUnavailableError()

    intent_runtime = UnavailableIntentRuntime(EMPTY_BUSINESS_INTENT_CATALOG)
    monkeypatch.setattr(
        sessions,
        "run_question_answer_graph",
        lambda **kwargs: graph_calls.append(kwargs),
    )
    app, _ = create_test_app_with_database(
        business_intent_catalog_runtime=intent_runtime
    )

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/sessions/s1/messages",
        json={"message": "分析银之杰"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "business_intent_catalog_unavailable"}
    }
    assert intent_runtime.snapshot_calls == 1
    assert intent_runtime.refresh_calls == 0
    assert graph_calls == []


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


def test_list_sessions_returns_only_principal_owned_sessions():
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as db:
        db.add_all(
            [
                AgentSession(
                    session_key="session_key_owned",
                    session_id="session_owned",
                    tenant_id="tenant_1",
                    user_id="user_1",
                    status="active",
                ),
                AgentSession(
                    session_key="session_key_other",
                    session_id="session_other",
                    tenant_id="tenant_1",
                    user_id="user_2",
                    status="active",
                ),
            ]
        )
        db.commit()

    response = TestClient(app).get("/api/sessions", params={"limit": 10})

    assert response.status_code == 200
    assert [item["session_id"] for item in response.json()["items"]] == [
        "session_owned"
    ]


def test_post_message_passes_only_recent_completed_visible_history(monkeypatch):
    captured = {}
    monkeypatch.setattr(sessions, "CONVERSATION_HISTORY_LIMIT", 4)

    def fake_graph(**kwargs):
        captured.update(kwargs)
        state = _completed_graph_state(kwargs)
        captured["state"] = state
        return state

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
        "旧问题2",
        "旧回答2",
        "旧问题3",
        "旧回答3",
    ]
    assert len(captured["history"]) == sessions.CONVERSATION_HISTORY_LIMIT
    assert captured["user_message_id"] not in {
        message.message_id for message in captured["history"]
    }
    assert [message.content for message in captured["state"]["messages"]] == [
        "旧问题2",
        "旧回答2",
        "旧问题3",
        "旧回答3",
        "当前问题",
    ]
    assert sum(
        message.message_id == captured["user_message_id"]
        for message in captured["state"]["messages"]
    ) == 1
    assert all("checkpoint" not in message.content for message in captured["history"])


def test_post_message_releases_turn_transaction_before_graph(monkeypatch):
    observed = {}

    attachment_context = {
        "asset_id": "asset-1",
        "analysis_id": "analysis-1",
        "media_type": "image/png",
        "width": 32,
        "height": 16,
        "status": "ready",
        "document_type": "chart",
        "summary": "附件快照",
        "extracted_text": "收入增长",
        "financial_metrics": [],
        "chart_observations": [],
        "entities": [],
        "risk_flags": [],
        "confidence": 0.9,
        "truncated": False,
    }

    class RecordingAttachmentResolver:
        def __init__(self):
            self.calls = 0

        def resolve(self, asset_ids, *, principal, session_id):
            self.calls += 1
            assert observed["db"].in_transaction()
            assert asset_ids == ["asset-1"]
            return [attachment_context]

    source_resolver = RecordingAttachmentResolver()

    def record_session(db):
        observed["db"] = db

    def fake_graph(**kwargs):
        observed["in_transaction"] = observed["db"].in_transaction()
        observed["store"] = kwargs["store"]
        observed["has_memory_retriever"] = "memory_retriever" in kwargs
        attachments = kwargs["attachment_resolver"].resolve(
            ["asset-1"],
            principal=Principal(
                tenant_id=kwargs["tenant_id"],
                user_id=kwargs["user_id"],
                scopes=tuple(kwargs["permission_scopes"]),
            ),
            session_id=kwargs["session_id"],
        )
        observed["attachment_summary"] = attachments[0]["summary"]
        return _completed_graph_state(kwargs)

    monkeypatch.setattr(sessions, "run_question_answer_graph", fake_graph)
    app, testing_session_local = create_test_app_with_database(on_session=record_session)
    app.dependency_overrides[sessions.get_attachment_context_resolver] = (
        lambda: source_resolver
    )
    response = TestClient(app).post(
        "/api/sessions/s1/messages",
        json={"message": "问题", "attachments": ["asset-1"]},
    )

    assert response.status_code == 200
    assert observed["in_transaction"] is False
    assert observed["store"] is TEST_MEMORY_STORE
    assert observed["has_memory_retriever"] is False
    assert observed["attachment_summary"] == "附件快照"
    assert source_resolver.calls == 1


@pytest.mark.parametrize("failure_stage", ["history", "attachment", "init"])
def test_post_message_pre_graph_failure_fails_turn_and_releases_session(
    monkeypatch,
    failure_stage,
):
    call_count = 0

    if failure_stage == "history":
        original = sessions.ConversationRepository.list_context_messages

        def fail_once(repository, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("history unavailable")
            return original(repository, **kwargs)

        monkeypatch.setattr(
            sessions.ConversationRepository,
            "list_context_messages",
            fail_once,
        )
    elif failure_stage == "init":
        original = sessions.build_graph_checkpointer

        def fail_once():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("checkpointer unavailable")
            return original()

        monkeypatch.setattr(sessions, "build_graph_checkpointer", fail_once)

    class FailOnceAttachmentResolver:
        def resolve(self, *_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if failure_stage == "attachment" and call_count == 1:
                raise RuntimeError("attachment unavailable")
            return []

    monkeypatch.setattr(
        sessions,
        "run_question_answer_graph",
        lambda **kwargs: _completed_graph_state(kwargs),
    )
    app, testing_session_local = create_test_app_with_database()
    app.dependency_overrides[sessions.get_attachment_context_resolver] = (
        FailOnceAttachmentResolver
    )
    client = TestClient(app, raise_server_exceptions=False)
    payload = {
        "message": "问题",
        "attachments": ["asset-1"] if failure_stage == "attachment" else [],
    }

    first = client.post(
        "/api/sessions/s1/messages",
        headers={"Idempotency-Key": f"failed-{failure_stage}"},
        json=payload,
    )
    second = client.post(
        "/api/sessions/s1/messages",
        headers={"Idempotency-Key": f"retry-{failure_stage}"},
        json=payload,
    )

    assert first.status_code == 500
    assert second.status_code == 200
    with testing_session_local() as db:
        session_record = db.scalars(select(AgentSession)).one()
        runs = list(db.scalars(select(AgentRun).order_by(AgentRun.created_at)))
        assistants = list(
            db.scalars(
                select(AgentMessage)
                .where(AgentMessage.role == "assistant")
                .order_by(AgentMessage.sequence_no)
            )
        )
    assert session_record.active_run_id is None
    assert [run.status for run in runs] == ["failed", "completed"]
    assert [message.status for message in assistants] == ["failed", "completed"]


def test_post_message_maps_database_error_to_safe_503(monkeypatch):
    def fail_begin_turn(*_args, **_kwargs):
        raise SQLAlchemyError("SELECT secret FROM credentials")

    monkeypatch.setattr(sessions.ConversationService, "begin_turn", fail_begin_turn)
    app, _ = create_test_app_with_database()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/sessions/s1/messages",
        json={"message": "问题"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "conversation_persistence_unavailable"}
    }
    assert "secret" not in response.text


def test_post_message_maps_completion_database_error_to_safe_503(monkeypatch):
    def fail_complete_turn(*_args, **_kwargs):
        raise SQLAlchemyError("UPDATE secret_credentials")

    monkeypatch.setattr(
        sessions.ConversationService,
        "complete_turn",
        fail_complete_turn,
    )
    monkeypatch.setattr(
        sessions,
        "run_question_answer_graph",
        lambda **kwargs: _completed_graph_state(kwargs),
    )
    app, testing_session_local = create_test_app_with_database()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/sessions/s1/messages",
        json={"message": "问题"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "conversation_persistence_unavailable"}
    }
    assert "secret" not in response.text
    with testing_session_local() as db:
        run = db.scalars(select(AgentRun)).one()
        assistant = db.scalar(
            select(AgentMessage).where(AgentMessage.role == "assistant")
        )
        session_record = db.scalars(select(AgentSession)).one()
    assert run.status == "failed"
    assert assistant.status == "failed"
    assert session_record.active_run_id is None


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
        captured["snapshot_contexts"] = kwargs["attachment_resolver"].resolve(
            kwargs["attachment_ids"],
            principal=Principal(
                tenant_id=kwargs["tenant_id"],
                user_id=kwargs["user_id"],
                scopes=tuple(kwargs["permission_scopes"]),
            ),
            session_id=kwargs["session_id"],
        )
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
    assert captured["resolved_asset_ids"] == ["asset-1"]
    assert captured["resolved_tenant_id"] == "tenant_1"
    assert captured["resolved_session_id"] == "s1"
    assert captured["attachment_ids"] == ["asset-1"]
    assert not isinstance(captured["attachment_resolver"], AttachmentResolver)
    assert captured["snapshot_contexts"][0]["summary"] == "图表摘要"


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


def test_post_session_message_flushes_trace_after_persisting_run(
    monkeypatch,
):
    monkeypatch.setattr(sessions.settings, "use_mock_rag", True)
    monkeypatch.setattr(sessions.settings, "trace_success_sample_ratio", 1.0)
    app, testing_session_local = create_test_app_with_database(
        business_intent_catalog_runtime=_TestBusinessIntentCatalogRuntime(
            EMPTY_BUSINESS_INTENT_CATALOG
        )
    )
    client = TestClient(app)

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "你好",
        },
    )

    assert response.status_code == 200
    with testing_session_local() as session:
        saved_run = session.scalars(select(AgentRun)).one()
        saved_events = session.scalars(select(AgentTraceEvent)).all()

    assert saved_run.session_id == "s1"
    assert saved_run.status == "completed"
    assert saved_run.trace_id == response.json()["trace_id"]
    assert saved_run.trace_capture_status == "captured"
    assert saved_events
    assert {event.run_id for event in saved_events} == {saved_run.run_id}


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


def test_post_session_message_syncs_committed_memory_candidate_to_store(monkeypatch):
    class RecordingStore:
        def __init__(self):
            self.calls = []

        def put(self, namespace, key, value):
            self.calls.append((namespace, key, value))

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
    store = RecordingStore()
    app.dependency_overrides[get_memory_store] = lambda: store
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
        saved = session.scalars(select(MemoryCandidateRecord)).one()

    assert saved.candidate_id == "memcand_1"
    assert saved.content == "所有的文档用中文"
    assert saved.store_sync_status == "synced"
    assert store.calls[0][1] == "memcand_1"


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
                    tenant_id=tenant_id,
                    approval_type="tool_call",
                    subject_name="mcp.mock.trade.submit",
                    risk_level="high",
                    action_digest=f"digest_{tenant_id}",
                    checkpoint_thread_id=f"run_{tenant_id}",
                    checkpoint_namespace="",
                    checkpoint_id=f"checkpoint_{tenant_id}",
                    interrupt_id=f"interrupt_{tenant_id}",
                    requested_by="request-user",
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
                tenant_id="tenant_1",
                approval_type="tool_call",
                subject_name="mcp.mock.trade.submit",
                risk_level="high",
                action_digest="digest_1",
                checkpoint_thread_id="run_1",
                checkpoint_namespace="",
                checkpoint_id="checkpoint_1",
                interrupt_id="interrupt_1",
                requested_by="user_1",
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


def test_approval_api_returns_minimal_projection_without_sensitive_payload():
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        session.add(
            ApprovalRecord(
                approval_id="approval_secret",
                run_id="run_1",
                tenant_id="tenant_1",
                approval_type="tool_call",
                subject_name="mcp.mock.secret_write",
                risk_level="high",
                action_digest="digest_secret",
                checkpoint_thread_id="run_1",
                checkpoint_namespace="",
                checkpoint_id="checkpoint_1",
                interrupt_id="interrupt_1",
                requested_by="user_1",
                status="pending",
            )
        )
        session.commit()

    response = TestClient(app).get(
        "/api/approvals/pending",
        params={"tenant_id": "tenant_1", "actor_id": "approver_1"},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert "payload" not in item
    assert "arguments" not in item
    assert item["action_digest"] == "digest_secret"


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
                    "checkpoint_namespace": "",
                    "status": "pending",
                }
            )
        ]
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", interrupted_graph)
    monkeypatch.setattr(
        sessions,
        "_interrupt_payloads",
        lambda state: [
            {
                "approval_id": "approval_1",
                "approval_type": "tool_call",
                "run_id": state["request_id"],
                "tenant_id": state["tenant_id"],
                "requested_by": state["user_id"],
                "subject_name": "mcp.mock.trade.submit",
                "risk_level": "high",
                "action_digest": "digest_1",
                "checkpoint_thread_id": build_checkpoint_thread_id(tenant_id=state["tenant_id"], session_key=state["session_key"], request_id=state["request_id"]),
                "checkpoint_namespace": "",
                "checkpoint_id": "checkpoint_1",
                "interrupt_id": "interrupt_1",
            }
        ],
    )
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
    assert approval.action_digest == "digest_1"
    assert approval.run_id == run.run_id
    assert approval.checkpoint_thread_id.startswith("cpr1_")
    assert run.status == "interrupted"


def test_approver_decision_preserves_checkpoint_request_user_scopes(monkeypatch):
    captured = {}
    intent_runtime = _TestBusinessIntentCatalogRuntime(EMPTY_BUSINESS_INTENT_CATALOG)

    def fake_resume(**kwargs):
        captured.update(kwargs)
        state = create_initial_state(
            session_id="session_1",
            session_key="session_key_1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="提交一笔模拟交易",
            assistant_message_id="assistant_run_1",
            permission_scopes=["mcp:mock", "rag:retrieve"],
        )
        state["request_id"] = "checkpoint_request_1"
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
        business_intent_catalog_runtime=intent_runtime
    )
    with testing_session_local() as session:
        expected_checkpoint = add_approval_resume_fixture(session)
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
    assert captured["checkpoint_thread_id"] == expected_checkpoint
    assert captured["approval_decision"] == {
        "approval_id": "approval_1",
        "action_digest": "digest_1",
        "decision": "approved",
    }
    assert captured["permission_scopes"]
    assert "rag:retrieve" in captured["permission_scopes"]
    assert captured["permission_scopes"] != [
        "agent:invoke",
        "agent:read",
        "approval:decide",
        "approval:read",
        "mcp:mock",
        "rag:retrieve",
    ]
    assert intent_runtime.snapshot_calls == 1
    assert intent_runtime.refresh_calls == 0
    assert captured["business_intent_catalog"] is intent_runtime.catalog
    assert response.json()["run_id"] == "run_1"
    assert response.json()["run_status"] == "completed"
    with testing_session_local() as session:
        approval = session.get(ApprovalRecord, "approval_1")
        run = session.get(AgentRun, "run_1")
        runs = list(session.scalars(select(AgentRun)))
    assert approval.status == "approved"
    assert approval.decided_by == "approver_1"
    assert run.status == "completed"
    assert [item.run_id for item in runs] == ["run_1"]


def test_approval_resume_rejects_different_existing_checkpoint_before_graph(
    monkeypatch,
):
    resume_calls = []
    other_checkpoint = build_checkpoint_thread_id(
        tenant_id="tenant_1",
        session_key="session_key_1",
        request_id="other_real_request",
    )
    checkpointer = InMemorySaver()
    sessions.run_question_answer_graph(
        business_intent_catalog=BusinessIntentCatalog([]),
        session_id="session_1",
        session_key="session_key_1",
        tenant_id="tenant_1",
        user_id="user_1",
        request_id="other_real_request",
        user_message="你好",
        checkpointer=checkpointer,
    )
    assert checkpointer.get_tuple(
        {
            "configurable": {
                "thread_id": other_checkpoint,
                "checkpoint_ns": "",
            }
        }
    ) is not None

    def forbidden_resume(**kwargs):
        assert kwargs["checkpoint_thread_id"] == other_checkpoint
        resume_calls.append(kwargs)
        raise AssertionError("身份校验失败时不得恢复另一个真实 checkpoint")

    monkeypatch.setattr(approvals, "resume_question_answer_graph", forbidden_resume)
    monkeypatch.setattr(approvals, "build_graph_checkpointer", lambda: checkpointer)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as db:
        add_approval_resume_fixture(
            db,
            checkpoint_thread_id=other_checkpoint,
        )
        db.commit()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/approvals/approval_1/decision",
        json={"actor_id": "approver_1", "decision": "approved"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approval_resume_identity_mismatch"
    assert resume_calls == []


def test_approval_resume_rejects_payload_identity_mismatch_before_graph(monkeypatch):
    resume_calls = []
    monkeypatch.setattr(
        approvals,
        "resume_question_answer_graph",
        lambda **kwargs: resume_calls.append(kwargs),
    )
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as db:
        add_approval_resume_fixture(
            db,
            payload_overrides={"run_id": "other_run"},
        )
        db.commit()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/approvals/approval_1/decision",
        json={"actor_id": "approver_1", "decision": "approved"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approval_resume_failed"
    assert resume_calls == []


def test_approval_claim_commit_failure_returns_safe_503(monkeypatch):
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as db:
        add_approval_resume_fixture(db)
        db.commit()

    def fail_commit(_db):
        raise SQLAlchemyError("COMMIT leaked_secret")

    monkeypatch.setattr(approvals.Session, "commit", fail_commit)
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/approvals/approval_1/decision",
        json={"actor_id": "approver_1", "decision": "approved"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "approval_persistence_unavailable"}
    }
    assert "leaked_secret" not in response.text


def test_approval_resume_fails_closed_when_request_user_tool_permission_revoked(
    monkeypatch,
):
    resume_scopes = []

    class RevokedRbacProvider:
        def resolve_principal(self, *, tenant_id, user_id):
            return Principal(
                tenant_id=tenant_id,
                user_id=user_id,
                scopes=("agent:invoke", "agent:read", "rag:retrieve"),
            )

        def tool_scopes(self, principal):
            return principal.scopes

    def deny_revoked_scope(**kwargs):
        resume_scopes.append(kwargs["permission_scopes"])
        raise AgentGraphExecutionError(
            node_name="tool_agent",
            message="工具权限不足。",
            code="tool_permission_denied",
            http_status=403,
        )

    monkeypatch.setattr(approvals, "resume_question_answer_graph", deny_revoked_scope)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    monkeypatch.setattr(
        approvals,
        "get_rbac_provider",
        lambda: RevokedRbacProvider(),
        raising=False,
    )
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as db:
        add_approval_resume_fixture(db)
        db.commit()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/approvals/approval_1/decision",
        json={"actor_id": "approver_1", "decision": "approved"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "tool_permission_denied"
    assert resume_scopes == [["agent:invoke", "agent:read", "rag:retrieve"]]
    with testing_session_local() as db:
        record = db.get(ApprovalRecord, "approval_1")
    assert record.status == "resume_failed"
    assert record.decision == "approved"


def test_approval_resume_fails_closed_when_request_user_rbac_is_unavailable(
    monkeypatch,
):
    class UnavailableRbacProvider:
        def resolve_principal(self, **_kwargs):
            raise RuntimeError("rbac unavailable")

    monkeypatch.setattr(
        approvals,
        "get_rbac_provider",
        lambda: UnavailableRbacProvider(),
    )
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as db:
        add_approval_resume_fixture(db)
        db.commit()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/approvals/approval_1/decision",
        json={"actor_id": "approver_1", "decision": "approved"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == (
        "approval_requester_authorization_unavailable"
    )


def test_approval_failure_record_failure_returns_safe_503(monkeypatch):
    original_error = AgentGraphExecutionError(
        node_name="approval_gate",
        message="工具参数已变化",
        code="approval_payload_mismatch",
        http_status=409,
    )

    def fail_resume(**_kwargs):
        raise original_error

    def fail_record(*_args, **_kwargs):
        raise SQLAlchemyError("UPDATE leaked_secret")

    monkeypatch.setattr(approvals, "resume_question_answer_graph", fail_resume)
    monkeypatch.setattr(approvals.ApprovalRepository, "fail_resume", fail_record)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as db:
        add_approval_resume_fixture(db)
        db.commit()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/approvals/approval_1/decision",
        json={"actor_id": "approver_1", "decision": "approved"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "approval_persistence_unavailable"}
    }
    assert "leaked_secret" not in response.text


@pytest.mark.parametrize(
    ("state_session_key", "state_request_id"),
    [
        ("wrong_session_key", "checkpoint_request_1"),
        ("session_key_1", "wrong_request_id"),
    ],
)
def test_approval_resume_rejects_state_identity_mismatch(
    monkeypatch,
    state_session_key,
    state_request_id,
):
    def fake_resume(**kwargs):
        state = create_initial_state(
            session_id="session_1",
            session_key=state_session_key,
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="提交一笔模拟交易",
            assistant_message_id="assistant_run_1",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = state_request_id
        state["trace_id"] = "trace_1"
        return state

    monkeypatch.setattr(approvals, "resume_question_answer_graph", fake_resume)
    monkeypatch.setattr(
        approvals,
        "_project_current_interrupts",
        lambda state: [{"approval_id": "approval_child", "approval_type": "tool_call", "run_id": "run_1", "tenant_id": "tenant_1", "requested_by": "user_1", "subject_name": "mcp.mock.trade.submit", "risk_level": "high", "action_digest": "digest_child", "checkpoint_thread_id": build_checkpoint_thread_id(tenant_id="tenant_1", session_key="session_key_1", request_id="checkpoint_request_1"), "checkpoint_namespace": "", "checkpoint_id": "checkpoint_2", "interrupt_id": "interrupt_2"}],
    )
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        add_approval_resume_fixture(session)
        session.commit()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/approvals/approval_1/decision",
        json={"actor_id": "approver_1", "decision": "approved"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approval_resume_identity_mismatch"
    with testing_session_local() as session:
        approval = session.get(ApprovalRecord, "approval_1")
        run = session.get(AgentRun, "run_1")
    assert approval.status == "resume_failed"
    assert run.status == "interrupted"


def test_approval_resume_child_approval_keeps_original_run_identity(monkeypatch):
    def fake_resume(**kwargs):
        state = create_initial_state(
            session_id="session_1",
            session_key="session_key_1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="提交另一笔模拟交易",
            assistant_message_id="assistant_run_1",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = "checkpoint_request_1"
        state["trace_id"] = "trace_1"
        state["__interrupt__"] = [
            Interrupt(
                value={
                    "approval_id": "approval_child",
                    "approval_type": "tool_call",
                    "run_id": "wrong_run",
                    "session_id": "wrong_session",
                    "session_key": "wrong_session_key",
                    "tenant_id": "wrong_tenant",
                    "request_user_id": "wrong_user",
                    "tool_name": "mcp.mock.trade.submit",
                    "tool_version": "1.0.0",
                    "arguments_hash": "hash_child",
                    "arguments": {"symbol": "000001"},
                    "purpose": "提交第二笔模拟交易",
                    "checkpoint_thread_id": "child_checkpoint",
                    "checkpoint_namespace": "",
                    "status": "pending",
                }
            )
        ]
        return state

    monkeypatch.setattr(approvals, "resume_question_answer_graph", fake_resume)
    monkeypatch.setattr(
        approvals,
        "_project_current_interrupts",
        lambda state: [{"approval_id": "approval_child", "approval_type": "tool_call", "run_id": "run_1", "tenant_id": "tenant_1", "requested_by": "user_1", "subject_name": "mcp.mock.trade.submit", "risk_level": "high", "action_digest": "digest_child", "checkpoint_thread_id": build_checkpoint_thread_id(tenant_id="tenant_1", session_key="session_key_1", request_id="checkpoint_request_1"), "checkpoint_namespace": "", "checkpoint_id": "checkpoint_2", "interrupt_id": "interrupt_2"}],
    )
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        expected_checkpoint = add_approval_resume_fixture(session)
        session.commit()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/approvals/approval_1/decision",
        json={"actor_id": "approver_1", "decision": "approved"},
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "run_1"
    with testing_session_local() as session:
        child = session.get(ApprovalRecord, "approval_child")
        runs = list(session.scalars(select(AgentRun)))
    assert child.run_id == "run_1"
    assert child.tenant_id == "tenant_1"
    assert child.requested_by == "user_1"
    assert child.checkpoint_thread_id == expected_checkpoint
    assert child.action_digest == "digest_child"
    assert [(run.run_id, run.status) for run in runs] == [("run_1", "interrupted")]


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
            assistant_message_id="assistant_run_1",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = "checkpoint_request_1"
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
        add_approval_resume_fixture(session)
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
        add_approval_resume_fixture(
            session,
            status="approved",
            decision="approved",
            decided_by="approver_1",
        )
        session.flush()
        session.get(AgentRun, "run_1").status = "completed"
        session.get(AgentMessage, "assistant_run_1").status = "completed"
        session.get(AgentSession, "session_key_1").active_run_id = None
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
    assert response.json()["request_id"] == "checkpoint_request_1"
    assert response.json()["message_id"] == "assistant_run_1"
    assert response.json()["final_response"]["content"] == ""
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
        add_approval_resume_fixture(session)
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
            assistant_message_id="assistant_run_1",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = "checkpoint_request_1"
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
        add_approval_resume_fixture(
            session,
            status="resume_failed",
            decision="approved",
            resume_attempts=1,
            last_resume_error="checkpoint missing",
            decided_by="approver_1",
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
                tenant_id="tenant_1",
                approval_type="tool_call",
                subject_name="mcp.mock.trade.submit",
                risk_level="high",
                action_digest="digest_1",
                checkpoint_thread_id="run_1",
                checkpoint_namespace="",
                checkpoint_id="checkpoint_1",
                interrupt_id="interrupt_1",
                requested_by="user_1",
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
            assistant_message_id="assistant_run_1",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = "checkpoint_request_1"
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
        add_approval_resume_fixture(session)
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


def test_approval_retry_flushes_each_run_buffer_outside_business_transaction(
    monkeypatch,
):
    resume_calls = 0
    observed_buffers = []

    def resume_with_trace_only_once(**kwargs):
        nonlocal resume_calls
        resume_calls += 1
        state = create_initial_state(
            session_id="session_1",
            session_key="session_key_1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="提交一笔模拟交易",
            assistant_message_id="assistant_run_1",
            permission_scopes=kwargs["permission_scopes"],
        )
        state["request_id"] = "checkpoint_request_1"
        state["trace_id"] = "trace_1"
        state["final_response"] = {
            "content": "模拟交易已提交。",
            "trace_id": "trace_1",
            "cited_evidence_ids": [],
            "approval_status": "approved",
        }
        observed_buffers.append(kwargs["trace_buffer"])
        if resume_calls == 1:
            kwargs["trace_buffer"].record_event(
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
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    monkeypatch.setattr(
        approvals.ApprovalRepository,
        "complete_resume",
        fail_first_finalization,
    )
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as session:
        add_approval_resume_fixture(session)
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
    with testing_session_local() as session:
        retry_run = session.get(AgentRun, "run_1")
        retry_assistant = session.get(AgentMessage, "assistant_run_1")
        assert retry_run.status == "interrupted"
        assert retry_assistant.status == "interrupted"
    second_response = client.post(
        "/api/approvals/approval_1/decision",
        json=request,
    )

    assert first_response.status_code == 409
    assert second_response.status_code == 200, second_response.text
    assert resume_calls == 2
    assert observed_buffers[0] is not observed_buffers[1]
    with testing_session_local() as session:
        saved_run = session.get(AgentRun, "run_1")
        saved_events = session.scalars(select(AgentTraceEvent)).all()
    assert saved_run.status == "completed"
    assert [(event.trace_id, event.run_id, event.node_name) for event in saved_events] == [
        ("trace_1", "run_1", "tool_agent")
    ]


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
    assert saved_run.trace_capture_status == "captured"
    assert saved_session.active_run_id is None
    assert saved_assistant.status == "failed"
    assert saved_assistant.metadata_json == {"error_code": "agent_execution_failed"}


def test_session_runs_use_isolated_buffers_and_flush_after_business_commit(
    monkeypatch,
):
    observed: list[TraceBuffer] = []

    def completed_graph(**kwargs):
        buffer = kwargs["trace_buffer"]
        state = _completed_graph_state(kwargs, content="已完成")
        state["intent"] = {"risk_level": "low"}
        state["verification"] = {"verdict": "pass"}
        buffer.record_event(
            TraceEvent(
                trace_id=state["trace_id"],
                node_name="final_response",
                status="success",
                latency_ms=1,
            )
        )
        observed.append(buffer)
        return state

    monkeypatch.setattr(sessions, "run_question_answer_graph", completed_graph)
    monkeypatch.setattr(sessions.settings, "trace_success_sample_ratio", 1)
    monkeypatch.setattr(sessions.settings, "trace_detailed_sample_ratio", 0)
    app, testing_session_local = create_test_app_with_database()
    client = TestClient(app)

    first = client.post(
        "/api/sessions/s1/messages",
        headers={"Idempotency-Key": "trace-run-1"},
        json={"message": "第一个问题"},
    )
    second = client.post(
        "/api/sessions/s2/messages",
        headers={"Idempotency-Key": "trace-run-2"},
        json={"message": "第二个问题"},
    )

    assert first.status_code == second.status_code == 200
    assert len(observed) == 2
    assert observed[0] is not observed[1]
    with testing_session_local() as db:
        runs = list(db.scalars(select(AgentRun)))
        events = list(db.scalars(select(AgentTraceEvent)))
    assert {run.trace_capture_status for run in runs} == {"captured"}
    assert {event.trace_id for event in events} == {
        first.json()["trace_id"],
        second.json()["trace_id"],
    }


def test_trace_flush_failure_does_not_change_completed_session_response(monkeypatch):
    def completed_graph(**kwargs):
        state = _completed_graph_state(kwargs, content="业务回答保持成功")
        state["verification"] = {"verdict": "pass"}
        return state

    class FailingTracePersistenceService:
        def __init__(self, **_kwargs):
            pass

        def persist_best_effort(self, **_kwargs):
            raise RuntimeError("trace backend unavailable")

    monkeypatch.setattr(sessions, "run_question_answer_graph", completed_graph)
    monkeypatch.setattr(
        sessions,
        "TracePersistenceService",
        FailingTracePersistenceService,
        raising=False,
    )
    app, testing_session_local = create_test_app_with_database()

    response = TestClient(app).post(
        "/api/sessions/s1/messages",
        json={"message": "问题"},
    )

    assert response.status_code == 200
    assert response.json()["content"] == "业务回答保持成功"
    with testing_session_local() as db:
        run = db.scalars(select(AgentRun)).one()
        assistant = db.scalar(
            select(AgentMessage).where(AgentMessage.role == "assistant")
        )
    assert run.status == "completed"
    assert assistant.status == "completed"


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
        run = session.scalars(select(AgentRun)).one()
    assert run.trace_capture_status == "captured"
    assert run.trace_capture_level == "detailed"


def test_post_session_message_stream_returns_answer_deltas_and_final(monkeypatch):
    def fake_stream(**kwargs):
        state = _initial_state_from_graph_kwargs(kwargs)
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
        kwargs["trace_buffer"].record_event(
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


def test_post_message_stream_uses_one_run_scoped_intent_catalog_snapshot(monkeypatch):
    captured = {}
    intent_runtime = _TestBusinessIntentCatalogRuntime(EMPTY_BUSINESS_INTENT_CATALOG)

    def fake_stream(**kwargs):
        captured.update(kwargs)
        yield _completed_graph_state(kwargs)

    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database(
        business_intent_catalog_runtime=intent_runtime
    )

    response = TestClient(app).post(
        "/api/sessions/s1/messages/stream",
        json={"message": "分析银之杰"},
    )

    assert response.status_code == 200
    assert intent_runtime.snapshot_calls == 1
    assert intent_runtime.refresh_calls == 0
    assert captured["business_intent_catalog"] is intent_runtime.catalog


def test_stream_persists_original_turn_and_passes_run_identity(monkeypatch):
    captured = {}

    def fake_stream(**kwargs):
        captured.update(kwargs)
        yield _completed_graph_state(kwargs, content="流式持久化回答")

    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, testing_session_local = create_test_app_with_database()

    response = TestClient(app).post(
        "/api/sessions/s1/messages/stream",
        headers={"Idempotency-Key": "stream-turn-1"},
        json={"message": "分析银之杰"},
    )

    assert response.status_code == 200
    with testing_session_local() as db:
        messages = list(db.scalars(select(AgentMessage).order_by(AgentMessage.sequence_no)))
        runs = list(db.scalars(select(AgentRun)))
        session_record = db.scalars(select(AgentSession)).one()
    assert len(messages) == 2
    assert len(runs) == 1
    assert captured["session_key"] == session_record.session_key
    assert captured["request_id"] == "stream-turn-1"
    assert captured["trace_id"] == runs[0].trace_id
    assert captured["user_message_id"] == messages[0].message_id
    assert captured["assistant_message_id"] == messages[1].message_id
    assert captured["history"] == []
    assert captured["store"] is TEST_MEMORY_STORE
    assert "memory_retriever" not in captured
    assert [message.status for message in messages] == ["completed", "completed"]
    assert messages[1].content == "流式持久化回答"
    assert runs[0].status == "completed"
    assert session_record.active_run_id is None

    final = next(
        json.loads(block.split("data: ", 1)[1])
        for block in response.text.split("\n\n")
        if block.startswith("event: final")
    )
    done = next(
        json.loads(block.split("data: ", 1)[1])
        for block in response.text.split("\n\n")
        if block.startswith("event: done")
    )
    expected_identity = {
        "run_id": runs[0].run_id,
        "request_id": "stream-turn-1",
        "message_id": messages[1].message_id,
    }
    assert {key: final[key] for key in expected_identity} == expected_identity
    assert {key: done[key] for key in expected_identity} == expected_identity


def test_stream_marks_assistant_streaming_before_answer_delta(monkeypatch):
    app, testing_session_local = create_test_app_with_database()

    def fake_stream(**kwargs):
        yield _completed_graph_state(kwargs, content="abcdef")

    observed_statuses = []

    def inspect_chunks(content, *, chunk_size=12):
        if content == "abcdef":
            with testing_session_local() as db:
                assistant = db.scalars(
                    select(AgentMessage).where(AgentMessage.role == "assistant")
                ).one()
                observed_statuses.append(assistant.status)
        for index in range(0, len(content), chunk_size):
            yield content[index : index + chunk_size]

    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    monkeypatch.setattr(sessions, "_chunk_text", inspect_chunks)

    response = TestClient(app).post(
        "/api/sessions/s1/messages/stream",
        headers={"Idempotency-Key": "stream-status-1"},
        json={"message": "问题"},
    )

    assert response.status_code == 200
    assert observed_statuses == ["streaming"]


def test_completed_stream_idempotency_replays_without_graph(monkeypatch):
    graph_calls = []

    def fake_stream(**kwargs):
        graph_calls.append(kwargs)
        yield _completed_graph_state(kwargs, content="稳定回答")

    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, testing_session_local = create_test_app_with_database()
    client = TestClient(app)
    headers = {"Idempotency-Key": "stream-replay-1"}

    first = client.post(
        "/api/sessions/s1/messages/stream",
        headers=headers,
        json={"message": "问题"},
    )
    second = client.post(
        "/api/sessions/s1/messages/stream",
        headers=headers,
        json={"message": "问题"},
    )

    assert first.status_code == second.status_code == 200
    assert len(graph_calls) == 1
    assert "event: final" in second.text
    assert "稳定回答" in second.text
    with testing_session_local() as db:
        assert len(list(db.scalars(select(AgentRun)))) == 1
        assert len(list(db.scalars(select(AgentMessage)))) == 2


def test_stream_graph_error_fails_original_turn_and_clears_active_run(monkeypatch):
    def failing_stream(**_kwargs):
        raise AgentGraphExecutionError(
            node_name="retriever_agent",
            message="rag unavailable",
            code="agent_graph_execution_failed",
        )
        yield

    monkeypatch.setattr(sessions, "stream_question_answer_graph", failing_stream)
    app, testing_session_local = create_test_app_with_database()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/sessions/s1/messages/stream",
        headers={"Idempotency-Key": "stream-failure-1"},
        json={"message": "问题"},
    )

    assert response.status_code == 200
    error = next(
        json.loads(block.split("data: ", 1)[1])
        for block in response.text.split("\n\n")
        if block.startswith("event: error")
    )
    with testing_session_local() as db:
        run = db.scalars(select(AgentRun)).one()
        assistant = db.scalars(
            select(AgentMessage).where(AgentMessage.role == "assistant")
        ).one()
        session_record = db.scalars(select(AgentSession)).one()
    assert run.status == assistant.status == "failed"
    assert run.trace_capture_status == "captured"
    assert session_record.active_run_id is None
    assert error["run_id"] == run.run_id
    assert error["request_id"] == "stream-failure-1"
    assert error["message_id"] == assistant.message_id


def test_closing_stream_generator_fails_started_turn(monkeypatch):
    graph_calls = []
    monkeypatch.setattr(
        sessions,
        "stream_question_answer_graph",
        lambda **kwargs: graph_calls.append(kwargs),
    )
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as db:
        generator = sessions._stream_agent_message(
            session_id="s1",
            request=sessions.SessionMessageRequest(message="问题"),
            principal=Principal(
                tenant_id="tenant_1",
                user_id="user_1",
                scopes=("agent:invoke",),
            ),
            db=db,
            attachment_resolver=None,
            memory_store=TEST_MEMORY_STORE,
            business_intent_catalog=BusinessIntentCatalog([]),
            request_id="stream-disconnect-1",
        )
        next(generator)
        generator.close()

    with testing_session_local() as db:
        run = db.scalars(select(AgentRun)).one()
        assistant = db.scalars(
            select(AgentMessage).where(AgentMessage.role == "assistant")
        ).one()
        session_record = db.scalars(select(AgentSession)).one()
    assert graph_calls == []
    assert run.status == assistant.status == "failed"
    assert run.trace_capture_status == "captured"
    assert session_record.active_run_id is None


def test_cancelled_stream_fails_started_turn(monkeypatch):
    def cancelled_stream(**_kwargs):
        raise asyncio.CancelledError
        yield

    monkeypatch.setattr(sessions, "stream_question_answer_graph", cancelled_stream)
    _, testing_session_local = create_test_app_with_database()
    with testing_session_local() as db:
        generator = sessions._stream_agent_message(
            session_id="s1",
            request=sessions.SessionMessageRequest(message="问题"),
            principal=Principal(
                tenant_id="tenant_1",
                user_id="user_1",
                scopes=("agent:invoke",),
            ),
            db=db,
            attachment_resolver=None,
            memory_store=TEST_MEMORY_STORE,
            business_intent_catalog=BusinessIntentCatalog([]),
            request_id="stream-cancelled-1",
        )
        with pytest.raises(asyncio.CancelledError):
            list(generator)

    with testing_session_local() as db:
        run = db.scalars(select(AgentRun)).one()
        assistant = db.scalars(
            select(AgentMessage).where(AgentMessage.role == "assistant")
        ).one()
        session_record = db.scalars(select(AgentSession)).one()
    assert run.status == assistant.status == "failed"
    assert run.trace_capture_status == "captured"
    assert session_record.active_run_id is None


def test_disconnect_after_business_completion_keeps_success_capture_outcome(
    monkeypatch,
):
    def completed_stream(**kwargs):
        state = _completed_graph_state(kwargs, content="已完成")
        state["verification"] = {"verdict": "pass"}
        yield state

    monkeypatch.setattr(sessions, "stream_question_answer_graph", completed_stream)
    monkeypatch.setattr(sessions.settings, "trace_success_sample_ratio", 0)
    monkeypatch.setattr(sessions.settings, "trace_detailed_sample_ratio", 0)
    _, testing_session_local = create_test_app_with_database()
    with testing_session_local() as db:
        generator = sessions._stream_agent_message(
            session_id="s1",
            request=sessions.SessionMessageRequest(message="问题"),
            principal=Principal(
                tenant_id="tenant_1",
                user_id="user_1",
                scopes=("agent:invoke",),
            ),
            db=db,
            attachment_resolver=None,
            memory_store=TEST_MEMORY_STORE,
            business_intent_catalog=BusinessIntentCatalog([]),
            request_id="stream-completed-disconnect-1",
        )
        for event in generator:
            if event.startswith("event: final"):
                generator.close()
                break

    with testing_session_local() as db:
        run = db.scalars(select(AgentRun)).one()
        assistant = db.scalars(
            select(AgentMessage).where(AgentMessage.role == "assistant")
        ).one()
    assert run.status == assistant.status == "completed"
    assert run.trace_capture_status == "not_sampled"


def test_in_progress_stream_replay_returns_stable_sse_conflict(monkeypatch):
    graph_calls = []
    monkeypatch.setattr(
        sessions,
        "stream_question_answer_graph",
        lambda **kwargs: graph_calls.append(kwargs),
    )
    app, testing_session_local = create_test_app_with_database()
    with testing_session_local() as db:
        handle = ConversationService(db).begin_turn(
            BeginTurn(
                tenant_id="tenant_1",
                user_id="user_1",
                session_id="s1",
                request_id="stream-running-1",
                user_content="问题",
            )
        )
        db.commit()

    response = TestClient(app).post(
        "/api/sessions/s1/messages/stream",
        headers={"Idempotency-Key": "stream-running-1"},
        json={"message": "问题"},
    )

    assert response.status_code == 200
    error = next(
        json.loads(block.split("data: ", 1)[1])
        for block in response.text.split("\n\n")
        if block.startswith("event: error")
    )
    assert error["code"] == "request_in_progress"
    assert error["http_status"] == 409
    assert error["run_id"] == handle.run_id
    assert error["request_id"] == handle.request_id
    assert error["message_id"] == handle.assistant_message_id
    assert graph_calls == []
    with testing_session_local() as db:
        assert db.get(AgentRun, handle.run_id).status == "running"
        assert db.get(AgentMessage, handle.assistant_message_id).status == "pending"


def test_stream_interrupt_reuses_turn_and_returns_stable_identity(monkeypatch):
    def interrupted_stream(**kwargs):
        state = _initial_state_from_graph_kwargs(kwargs)
        state["__interrupt__"] = [
            Interrupt(
                value={
                    "approval_id": "stream_approval_1",
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
                    "checkpoint_thread_id": build_checkpoint_thread_id(
                        tenant_id=state["tenant_id"],
                        session_key=state["session_key"],
                        request_id=state["request_id"],
                    ),
                    "checkpoint_namespace": "",
                    "status": "pending",
                }
            )
        ]
        yield state

    monkeypatch.setattr(sessions, "stream_question_answer_graph", interrupted_stream)
    monkeypatch.setattr(sessions, "_interrupt_payloads", lambda state: [{"approval_id": "stream_approval_1", "approval_type": "tool_call", "run_id": state["request_id"], "tenant_id": state["tenant_id"], "requested_by": state["user_id"], "subject_name": "mcp.mock.trade.submit", "risk_level": "high", "action_digest": "digest_stream", "checkpoint_thread_id": build_checkpoint_thread_id(tenant_id=state["tenant_id"], session_key=state["session_key"], request_id=state["request_id"]), "checkpoint_namespace": "", "checkpoint_id": "checkpoint_stream", "interrupt_id": "interrupt_stream"}])
    app, testing_session_local = create_test_app_with_database()

    response = TestClient(app).post(
        "/api/sessions/s1/messages/stream",
        headers={"Idempotency-Key": "stream-interrupt-1"},
        json={"message": "提交交易"},
    )

    approval_event = next(
        json.loads(block.split("data: ", 1)[1])
        for block in response.text.split("\n\n")
        if block.startswith("event: approval_required")
    )
    with testing_session_local() as db:
        run = db.scalars(select(AgentRun)).one()
        assistant = db.scalars(
            select(AgentMessage).where(AgentMessage.role == "assistant")
        ).one()
        session_record = db.scalars(select(AgentSession)).one()
    assert run.status == assistant.status == "interrupted"
    assert run.trace_capture_status == "captured"
    assert session_record.active_run_id == run.run_id
    assert approval_event["run_id"] == run.run_id
    assert approval_event["request_id"] == "stream-interrupt-1"
    assert approval_event["message_id"] == assistant.message_id


def test_stream_passes_completed_history_to_next_turn(monkeypatch):
    captured_histories = []

    def fake_stream(**kwargs):
        captured_histories.append(kwargs["history"])
        yield _completed_graph_state(kwargs, content=f"回答{len(captured_histories)}")

    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    first = client.post(
        "/api/sessions/s1/messages/stream",
        headers={"Idempotency-Key": "stream-history-1"},
        json={"message": "第一问"},
    )
    second = client.post(
        "/api/sessions/s1/messages/stream",
        headers={"Idempotency-Key": "stream-history-2"},
        json={"message": "第二问"},
    )

    assert first.status_code == second.status_code == 200
    assert captured_histories[0] == []
    assert [(message.role, message.content) for message in captured_histories[1]] == [
        ("user", "第一问"),
        ("assistant", "回答1"),
    ]


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
        state = _initial_state_from_graph_kwargs(kwargs)
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
        state = _initial_state_from_graph_kwargs(kwargs)
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
        state = _initial_state_from_graph_kwargs(kwargs)
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
        kwargs["trace_buffer"].record_event(
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
        state = _initial_state_from_graph_kwargs(kwargs)
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
        state = _initial_state_from_graph_kwargs(kwargs)
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
        state = _initial_state_from_graph_kwargs(kwargs)
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
        state = _initial_state_from_graph_kwargs(kwargs)
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
    assert 'event: done\ndata: {"ok": false,' in body


def test_post_session_message_stream_returns_error_when_persistence_fails(
    monkeypatch,
):
    def fake_stream(**kwargs):
        state = _initial_state_from_graph_kwargs(kwargs)
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
    assert 'event: done\ndata: {"ok": false,' in body
    assert "event: final" not in body


def test_post_session_message_stream_returns_a_share_research_answer(monkeypatch):
    def fake_stream(**kwargs):
        state = _initial_state_from_graph_kwargs(kwargs)
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
    class AttachmentResolver:
        def resolve(self, *_args, **_kwargs):
            return []

    def fake_stream(**kwargs):
        state = _initial_state_from_graph_kwargs(kwargs)
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
    app.dependency_overrides[sessions.get_attachment_context_resolver] = (
        lambda: AttachmentResolver()
    )
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
