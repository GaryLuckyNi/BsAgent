"""任务权限上限与撤权测试；权限目录、任务和工具使用真实本地实现。"""

import asyncio
from importlib import import_module
from typing import ClassVar

import pytest
from app.conversations.service import BeginTurn, ConversationService
from app.core.config import settings
from app.schemas.tools import ToolCallRequest
from app.security import rbac
from app.security.directory import DirectoryRbacProvider, PermissionDirectory
from app.storage.models import Base
from app.tests.unit.test_tool_agent import EchoTool, _context
from app.tools.execution_gateway import ToolExecutionGateway
from app.tools.registry import ToolRegistry
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ISSUER = "https://issuer.example.test"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'authorization.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    directory = PermissionDirectory(factory)
    user_id = directory.grant(
        issuer=ISSUER,
        external_subject="subject",
        tenant_id="t",
        roles=(),
        permission_scopes=("agent:invoke", "echo:call"),
        actor_ref="test",
    )
    provider = DirectoryRbacProvider(directory, issuer=ISSUER)
    monkeypatch.setattr(settings, "rbac_backend", "principal")
    monkeypatch.setattr(settings, "jwt_issuer", ISSUER)
    monkeypatch.setattr(rbac, "get_rbac_provider", lambda: provider)
    principal = provider.resolve_principal(tenant_id="t", user_id=user_id)
    with factory() as db:
        handle = ConversationService(db).begin_turn(
            BeginTurn(
                tenant_id="t", user_id=user_id, session_id="s", request_id="r", user_content="测试"
            )
        )
        db.commit()
    yield factory, directory, provider, principal, handle
    engine.dispose()


def module():
    try:
        return import_module("app.security.run_authorization")
    except ModuleNotFoundError:
        pytest.fail("任务授权边界尚未实现", pytrace=False)


def initialize(env):
    factory, _, _, principal, handle = env
    with factory() as db:
        module().initialize_run(db, handle=handle, principal=principal)
        db.commit()


def change(env, scopes):
    env[1].grant(
        issuer=ISSUER,
        external_subject="subject",
        tenant_id="t",
        roles=(),
        permission_scopes=scopes,
        actor_ref="test",
    )


def test_resume_new_grants_do_not_expand_original_ceiling(environment):
    initialize(environment)
    change(environment, ("agent:invoke", "echo:call", "new:write"))
    factory, _, provider, principal, handle = environment
    with factory() as db:
        current = provider.resolve_principal(tenant_id="t", user_id=principal.user_id)
        refreshed = module().refresh_run(db, run_id=handle.run_id, principal=current)
        assert set(refreshed.scopes) == {"agent:invoke", "echo:call"}
        with pytest.raises(module().RunAuthorizationDenied):
            module().initialize_run(db, handle=handle, principal=current)


def test_revoked_membership_blocks_refresh(environment):
    initialize(environment)
    factory, directory, _, principal, handle = environment
    directory.revoke_membership(tenant_id="t", user_id=principal.user_id, actor_ref="test")
    with factory() as db, pytest.raises(module().RunAuthorizationDenied):
        module().refresh_run(db, run_id=handle.run_id)


def test_legacy_run_without_ceiling_is_denied(environment):
    with environment[0]() as db, pytest.raises(module().RunAuthorizationDenied):
        module().refresh_run(db, run_id=environment[4].run_id)


def test_gateway_requires_trusted_binding_even_if_model_claims_all_scopes(environment):
    calls = []

    class Counting(EchoTool):
        permission_scope: ClassVar[list[str]] = ["echo:call"]

        async def run(self, data):
            calls.append(data)
            return await super().run(data)

    registry = ToolRegistry()
    registry.register(Counting())
    result = asyncio.run(
        ToolExecutionGateway(registry).execute(
            ToolCallRequest(tool_name="echo", arguments={"text": "x"}, purpose="测试"),
            trace_id="trace",
            context=_context(permission_scopes=["*"]),
        )
    )
    assert result.status == "denied"
    assert result.execution_state == "not_started"
    assert calls == []


def binding_context(env, **overrides):
    return _context(permission_scopes=["agent:invoke", "echo:call", "new:write"]).model_copy(
        update={
            "tenant_id": "t",
            "user_id": env[3].user_id,
            "session_id": "s",
            "request_id": "r",
            **overrides,
        }
    )


def control_for(env):
    from app.conversations.durable import DurableRunService
    from app.conversations.execution import ExecutionControl

    with env[0]() as db:
        DurableRunService(db).prepare(handle=env[4], payload={})
        owner = DurableRunService(db).claim(env[4].run_id)
        db.commit()
    return ExecutionControl(env[0], env[4].run_id, owner)


def execute_tool(env, *, phase=None, scope="echo:call", context=None):
    from app.conversations.execution import execution_control

    calls = []

    class Counting(EchoTool):
        permission_scope: ClassVar[list[str]] = [scope]

        async def run(self, data):
            calls.append(data.text)
            if phase == "during_tool":
                change(env, ("agent:invoke",))
            return await super().run(data)

    registry = ToolRegistry()
    registry.register(Counting())

    def availability():
        if phase == "availability":
            change(env, ("agent:invoke",))
        return registry

    control = control_for(env)
    if phase == "receipt":
        original = control.tool_started

        def started(*args, **kwargs):
            receipt = original(*args, **kwargs)
            change(env, ("agent:invoke",))
            return receipt

        object.__setattr__(control, "tool_started", started)
    with execution_control(control):
        result = asyncio.run(
            ToolExecutionGateway(registry, availability_registry_factory=availability).execute(
                ToolCallRequest(tool_name="echo", arguments={"text": "x"}, purpose="测试"),
                trace_id=env[4].trace_id,
                context=context or binding_context(env),
            )
        )
    return result, calls


@pytest.mark.parametrize("phase", ["availability", "receipt"])
def test_revocation_before_dispatch_preserves_not_started_receipt(environment, phase):
    from app.storage.models import AgentRunToolReceipt
    from sqlalchemy import select

    initialize(environment)
    result, calls = execute_tool(environment, phase=phase)
    assert result.status == "denied"
    assert result.execution_state == "not_started"
    assert calls == []
    with environment[0]() as db:
        receipt = db.scalar(select(AgentRunToolReceipt))
        assert receipt.status == "settled"
        assert receipt.result_summary["execution_state"] == "not_started"


def test_revocation_during_tool_does_not_discard_real_result(environment):
    from app.storage.models import AgentRunToolReceipt
    from sqlalchemy import select

    initialize(environment)
    result, calls = execute_tool(environment, phase="during_tool")
    assert result.status == "success"
    assert calls == ["x"]
    with environment[0]() as db:
        receipt = db.scalar(select(AgentRunToolReceipt))
        assert receipt.status == "settled"
        assert receipt.result_summary["result"] == {"text": "x"}


def test_added_grant_cannot_enable_new_tool_on_old_run(environment):
    initialize(environment)
    change(environment, ("agent:invoke", "echo:call", "new:write"))
    result, calls = execute_tool(environment, scope="new:write")
    assert result.status == "denied"
    assert calls == []


@pytest.mark.parametrize(
    "tampered",
    [
        {"tenant_id": "other"},
        {"user_id": "other"},
        {"session_id": "other"},
        {"request_id": "other"},
    ],
)
def test_model_identity_cannot_select_authority(environment, tampered):
    initialize(environment)
    result, calls = execute_tool(environment, context=binding_context(environment, **tampered))
    assert result.status == "denied"
    assert calls == []


def test_graph_resume_clamps_scopes_and_keeps_binding_only_during_iteration(environment):
    initialize(environment)
    change(environment, ("agent:invoke", "echo:call", "new:write"))
    seen = []

    def graph(**kwargs):
        seen.append(kwargs["permission_scopes"])
        assert (
            "new:write"
            not in module().refresh_tool_context(binding_context(environment)).permission_scopes
        )
        yield {"ok": True}

    with environment[0]() as db:
        stream = module().authorized_graph(
            db, run_id=environment[4].run_id, principal=environment[3], function=graph
        )
        assert next(stream) == {"ok": True}
        with pytest.raises(module().RunAuthorizationDenied):
            module().refresh_tool_context(binding_context(environment))
        stream.close()
    assert seen == [["agent:invoke", "echo:call"]]


def test_directory_failure_blocks_gateway(environment, monkeypatch):
    from app.security.directory import PermissionDirectoryUnavailable

    initialize(environment)

    def unavailable(**_):
        raise PermissionDirectoryUnavailable

    monkeypatch.setattr(environment[2], "resolve_principal", unavailable)
    result, calls = execute_tool(environment)
    assert result.error.code == "run_authorization_unavailable"
    assert calls == []


def test_worker_rechecks_queue_time_revocation_before_graph(environment, monkeypatch):
    from dataclasses import asdict

    from app.conversations.dispatcher import execute_local_turn
    from app.conversations.durable import DurableRunService
    from app.storage.models import AgentRun, AgentRunProtocolEvent
    from sqlalchemy import select

    initialize(environment)
    factory, directory, _, principal, handle = environment
    with factory() as db:
        DurableRunService(db).prepare(
            handle=handle,
            payload={
                "handle": handle.model_dump(mode="json"),
                "principal": asdict(principal),
                "catalog": [],
            },
        )
        db.commit()
    directory.revoke_membership(tenant_id="t", user_id=principal.user_id, actor_ref="test")
    calls = []
    from app.api import sessions

    monkeypatch.setattr(sessions, "_stream_agent_message", lambda **_: calls.append("graph"))
    execute_local_turn(session_factory=factory, run_id=handle.run_id, memory_store=None)
    assert calls == []
    with factory() as db:
        assert db.get(AgentRun, handle.run_id).status == "failed"
        errors = list(
            db.scalars(select(AgentRunProtocolEvent).where(AgentRunProtocolEvent.event == "error"))
        )
        assert any(e.data.get("code") == "run_authorization_denied" for e in errors)


@pytest.mark.parametrize("endpoint", ["messages", "messages/stream", "messages/protocol-stream"])
def test_http_entry_binds_ceiling_and_tool_authority(environment, monkeypatch, endpoint):
    from app.api import sessions
    from app.graph import runtime
    from app.security.dependencies import get_current_principal
    from app.storage.database import get_db
    from app.storage.models import AgentRun, SecurityRunAuthorizationRecord
    from app.tests.integration.test_session_api import (
        _completed_graph_state,
        create_test_app_with_database,
    )
    from app.tests.support.workflow_llm import WorkflowTestLlm
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver
    from sqlalchemy import select

    app, _ = create_test_app_with_database()
    factory, _, _, principal, _handle = environment
    app.dependency_overrides[get_current_principal] = lambda: principal

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    monkeypatch.setattr(sessions, "maybe_build_llm_client", WorkflowTestLlm)
    monkeypatch.setattr(sessions, "build_graph_checkpointer", InMemorySaver)
    monkeypatch.setattr(runtime, "get_default_tool_registry", ToolRegistry)

    def complete(**kwargs):
        context = binding_context(
            environment, session_id="http-new", request_id=kwargs["request_id"]
        )
        assert "echo:call" in module().refresh_tool_context(context).permission_scopes
        return _completed_graph_state(kwargs)

    monkeypatch.setattr(sessions, "run_question_answer_graph", complete)
    monkeypatch.setattr(
        sessions, "stream_question_answer_graph", lambda **kwargs: iter([complete(**kwargs)])
    )
    payload = (
        {"message": "测试"}
        if endpoint != "messages/protocol-stream"
        else {"input": {"messages": [{"type": "human", "content": "测试"}], "request_id": "http-r"}}
    )
    response = TestClient(app).post(f"/api/sessions/http-new/{endpoint}", json=payload)
    assert response.status_code == 200, response.text
    with factory() as db:
        run = db.scalar(select(AgentRun).where(AgentRun.session_id == "http-new"))
        assert run.status == "completed", response.text
        assert db.get(SecurityRunAuthorizationRecord, run.run_id) is not None


def test_user_choice_resume_uses_initial_ceiling(environment, monkeypatch):
    from app.api import sessions
    from app.conversations.durable import DurableRunService
    from app.intent.catalog import BusinessIntentCatalog
    from app.tests.integration.test_session_api import _completed_graph_state
    from app.tests.support.workflow_llm import WorkflowTestLlm

    initialize(environment)
    change(environment, ("agent:invoke", "echo:call", "new:write"))
    factory, _, _, principal, handle = environment
    with factory() as db:
        service = ConversationService(db)
        DurableRunService(db).prepare(handle=handle, payload={})
        service.interrupt_turn(tenant_id="t", run_id=handle.run_id)
        db.commit()
    seen = []

    def resume(**kwargs):
        seen.extend(kwargs["permission_scopes"])
        state = _completed_graph_state(
            {
                "session_id": "s",
                "session_key": handle.session_key,
                "tenant_id": "t",
                "user_id": principal.user_id,
                "request_id": "r",
                "trace_id": handle.trace_id,
                "user_message": "测试",
                "user_message_id": handle.user_message_id,
                "assistant_message_id": handle.assistant_message_id,
            }
        )
        return state

    monkeypatch.setattr(sessions, "resume_user_input_graph", resume)
    monkeypatch.setattr(sessions, "maybe_build_llm_client", WorkflowTestLlm)
    monkeypatch.setattr(sessions, "build_graph_checkpointer", lambda: None)
    with factory() as db:
        handle = sessions._claim_user_input_resume(db=db, principal=principal, session_id="s")
        list(
            sessions._stream_user_input_resume(
                db=db,
                principal=principal,
                handle=handle,
                resume=sessions._ProtocolUserInputResume(
                    interrupt_id="i", interaction_id="i", input_type="selection", value="x"
                ),
                memory_store=None,
                business_intent_catalog=BusinessIntentCatalog([]),
            )
        )
    assert set(seen) == {"agent:invoke", "echo:call"}


@pytest.mark.parametrize("streaming", [False, True])
def test_denied_start_on_existing_sqlite_session_leaves_no_active_run(environment, streaming):
    from app.api import sessions
    from app.conversations.repository import ConversationRepository
    from app.intent.catalog import BusinessIntentCatalog
    from app.storage.models import AgentRun, AgentSession
    from sqlalchemy import select

    factory, _, _, principal, _ = environment
    with factory() as db:
        session = ConversationRepository(db).get_or_create_session(
            tenant_id="t", user_id=principal.user_id, session_id="existing-empty"
        )
        session_key = session.session_key
        db.commit()
    change(environment, ("echo:call",))
    with factory() as db:
        call = (
            sessions._stream_agent_message if streaming else sessions._run_and_persist_agent_message
        )
        try:
            result = call(
                db=db,
                session_id="existing-empty",
                principal=principal,
                request=sessions.SessionMessageRequest(message="测试"),
                request_id="denied-r",
                attachment_resolver=None,
                memory_store=None,
                business_intent_catalog=BusinessIntentCatalog([]),
            )
            if streaming:
                list(result)
        except module().RunAuthorizationDenied:
            pass
        db.rollback()
    with factory() as db:
        assert db.scalar(select(AgentRun).where(AgentRun.session_id == "existing-empty")) is None
        assert db.get(AgentSession, session_key).active_run_id is None


def test_approval_requester_scopes_are_limited_by_run_ceiling(environment, monkeypatch):
    from types import SimpleNamespace

    from app.api import approvals

    initialize(environment)
    change(environment, ("agent:invoke", "echo:call", "new:write"))
    monkeypatch.setattr(approvals, "get_rbac_provider", lambda: environment[2])
    record = SimpleNamespace(
        tenant_id="t", requested_by=environment[3].user_id, run_id=environment[4].run_id
    )
    with environment[0]() as db:
        scopes = approvals._resolve_resume_permission_scopes(record, db=db)
    assert set(scopes) == {"agent:invoke", "echo:call"}


def test_subject_disabled_blocks_old_task(environment):
    initialize(environment)
    environment[1].set_subject_enabled(
        issuer=ISSUER, external_subject="subject", enabled=False, actor_ref="test"
    )
    with environment[0]() as db, pytest.raises(module().RunAuthorizationDenied):
        module().refresh_run(db, run_id=environment[4].run_id)


def test_task_ceiling_cannot_move_between_issuers(environment, monkeypatch):
    initialize(environment)
    monkeypatch.setattr(settings, "jwt_issuer", "https://another-issuer.example.test")
    with environment[0]() as db, pytest.raises(module().RunAuthorizationDenied):
        module().refresh_run(db, run_id=environment[4].run_id)


def test_revoked_user_choice_does_not_claim_resume(environment):
    from app.api import sessions
    from app.storage.models import AgentRun

    initialize(environment)
    with environment[0]() as db:
        ConversationService(db).interrupt_turn(tenant_id="t", run_id=environment[4].run_id)
        db.commit()
    change(environment, ("echo:call",))
    with environment[0]() as db, pytest.raises(module().RunAuthorizationDenied):
        sessions._claim_user_input_resume(db=db, principal=environment[3], session_id="s")
    with environment[0]() as db:
        assert db.get(AgentRun, environment[4].run_id).status == "interrupted"
