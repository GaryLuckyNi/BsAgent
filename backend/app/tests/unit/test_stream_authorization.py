"""长连接授权使用真实目录和临时数据库，所有正文均为合成公开内容。"""

import importlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.conversations.durable import DurableRunService
from app.conversations.execution import subscribe_events
from app.conversations.service import BeginTurn, ConversationService
from app.core.config import settings
from app.security import rbac
from app.security.dependencies import get_current_principal
from app.security.directory import DirectoryRbacProvider, PermissionDirectory
from app.storage.models import AgentRun, AgentSession
from app.tests.integration.test_session_api import create_test_app_with_database
from fastapi.testclient import TestClient

ISSUER = "https://stream-identity.example.test"
BODY = "公开财报 600519.SH"
CANARY = "STREAM_AUTH_EXCEPTION_CANARY"


def module():
    return importlib.import_module("app.security.stream_authorization")


@pytest.fixture
def env(monkeypatch):
    app, factory = create_test_app_with_database(override_principal=False)
    directory = PermissionDirectory(factory)
    user_id = directory.grant(
        issuer=ISSUER,
        external_subject="stream-user",
        tenant_id="tenant_1",
        permission_scopes=("agent:read", "agent:invoke"),
        actor_ref="test",
    )
    provider = DirectoryRbacProvider(directory, issuer=ISSUER)
    principal = provider.resolve_principal(tenant_id="tenant_1", user_id=user_id)
    monkeypatch.setattr(settings, "auth_provider", "mock")
    monkeypatch.setattr(settings, "rbac_backend", "principal")
    monkeypatch.setattr(settings, "jwt_issuer", ISSUER)
    monkeypatch.setattr(rbac, "get_rbac_provider", lambda: provider)
    app.dependency_overrides[get_current_principal] = lambda: principal
    with factory() as db:
        service = ConversationService(db)
        handle = service.begin_turn(
            BeginTurn(
                tenant_id="tenant_1",
                user_id=user_id,
                session_id="s1",
                request_id="subscription-test",
                user_content="公开问题",
            )
        )
        durable = DurableRunService(db)
        durable.prepare(handle=handle, payload={})
        owner = durable.claim(handle.run_id)
        durable.append(
            handle.run_id,
            owner,
            "messages",
            [
                {"type": "ai", "id": handle.assistant_message_id, "content": BODY},
                {},
            ],
        )
        service.complete_turn(tenant_id="tenant_1", run_id=handle.run_id, content=BODY)
        durable.publish_status(handle.run_id, owner)
        db.commit()
    return SimpleNamespace(
        app=app,
        factory=factory,
        directory=directory,
        provider=provider,
        principal=principal,
        handle=handle,
    )


def guard(env, **kwargs):
    return module().StreamAuthorization(
        session_factory=env.factory,
        principal=env.principal,
        session_id="s1",
        run_id=env.handle.run_id,
        permissions=("agent:read",),
        **kwargs,
    )


def checked_stream(env, authorization=None):
    return module().authorized_event_stream(
        subscribe_events(session_factory=env.factory, run_id=env.handle.run_id),
        authorization=authorization or guard(env),
    )


@pytest.mark.parametrize("change", ["membership", "subject", "permission", "session"])
def test_revoke_between_frames_already_loaded_in_one_batch_stops_body(env, change):
    stream = checked_stream(env)
    assert "run_started" in next(stream)
    if change == "membership":
        env.directory.revoke_membership(
            user_id=env.principal.user_id, tenant_id="tenant_1", actor_ref="test"
        )
    elif change == "subject":
        env.directory.set_subject_enabled(
            issuer=ISSUER, external_subject="stream-user", enabled=False, actor_ref="test"
        )
    elif change == "permission":
        env.directory.grant(
            issuer=ISSUER,
            external_subject="stream-user",
            tenant_id="tenant_1",
            permission_scopes=("agent:invoke",),
            actor_ref="test",
        )
    else:
        with env.factory() as db:
            db.get(AgentSession, env.handle.session_key).status = "deleted"
            db.commit()
    remaining = "".join(stream)
    assert BODY not in remaining
    assert "stream_access_denied" in remaining
    assert "id:" not in remaining and "run_status" not in remaining
    with env.factory() as db:
        assert db.get(AgentRun, env.handle.run_id).status == "completed"
        assert len(DurableRunService(db).events(env.handle.run_id, after=0)) == 3


def test_normal_checked_subscription_retains_frames_and_does_not_mutate_events(env):
    expected = list(subscribe_events(session_factory=env.factory, run_id=env.handle.run_id))
    assert list(checked_stream(env)) == expected


def test_directory_and_resource_recheck_share_one_query(env):
    from sqlalchemy import event
    engine = env.factory.kw["bind"]
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        guard(env).check()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) == 1


def test_revoke_during_source_advance_checks_before_publication_and_closes_source(env):
    closed = []

    def frames():
        try:
            env.directory.revoke_membership(
                user_id=env.principal.user_id, tenant_id="tenant_1", actor_ref="test"
            )
            yield BODY
        finally:
            closed.append(True)

    result = "".join(module().authorized_event_stream(frames(), authorization=guard(env)))
    assert BODY not in result and "stream_access_denied" in result
    assert closed == [True]


def test_check_failure_has_fixed_error_without_original_exception_chain(env, monkeypatch):
    def unavailable(**kwargs):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(env.provider, "resolve_principal", unavailable)
    with pytest.raises(module().StreamAccessUnavailable) as error:
        guard(env).check()
    assert error.value.__cause__ is None and error.value.__context__ is None
    assert CANARY not in str(error.value)
    assert CANARY not in "".join(checked_stream(env))


def test_deadline_is_checked_after_source_advance(env):
    clock = [100.0]
    env.principal = replace(env.principal, expires_at=101.0)
    authorization = guard(env, clock=lambda: clock[0])

    def frames():
        clock[0] = 101.0
        yield BODY

    result = "".join(module().authorized_event_stream(frames(), authorization=authorization))
    assert BODY not in result and "stream_access_expired" in result


def test_deadline_is_rechecked_after_slow_authorization_lookup(env, monkeypatch):
    clock = [100.0]
    env.principal = replace(env.principal, expires_at=101.0)
    authorization = guard(env, clock=lambda: clock[0])
    original = env.provider.resolve_principal
    calls = []
    def delayed(**kwargs):
        current = original(**kwargs)
        calls.append(True)
        if len(calls) == 2:  # 发布前的那次目录查询跨过到期时间。
            clock[0] = 101.0
        return current
    monkeypatch.setattr(env.provider, "resolve_principal", delayed)
    result = "".join(module().authorized_event_stream(iter([BODY]), authorization=authorization))
    assert BODY not in result and "stream_access_expired" in result


def test_http_event_response_also_checks_revocation_after_route_auth(env, monkeypatch):
    from app.api import runs

    original = runs._response

    def revoke_before_subscription(*args, **kwargs):
        env.directory.revoke_membership(
            user_id=env.principal.user_id, tenant_id="tenant_1", actor_ref="test"
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(runs, "_response", revoke_before_subscription)
    response = TestClient(env.app).get(f"/api/sessions/s1/runs/{env.handle.run_id}/events")
    assert response.status_code == 200
    assert BODY not in response.text and "stream_access_denied" in response.text
    assert "RunSubscriptionAuthorizationError" in response.text


@pytest.mark.parametrize("protocol", ["legacy", "ag-ui"])
def test_other_stream_routes_recheck_after_generator_advance(env, monkeypatch, protocol):
    from app.api import sessions

    def source(**kwargs):
        yield sessions._sse("status", {"stage": "accepted", "run_id": env.handle.run_id})
        env.directory.revoke_membership(
            user_id=env.principal.user_id, tenant_id="tenant_1", actor_ref="test"
        )
        yield sessions._sse("answer_delta", {"content": BODY})

    monkeypatch.setattr(sessions, "_stream_agent_message", source)
    client = TestClient(env.app)
    response = client.post(
        "/api/sessions/s1/messages/stream" if protocol == "legacy" else "/api/ag-ui",
        json={"message": "公开问题"}
        if protocol == "legacy"
        else {
            "threadId": "s1",
            "runId": "stream-test",
            "messages": [
                {"id": "u1", "role": "user", "content": "公开问题"},
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
    )
    assert response.status_code == 200
    assert BODY not in response.text and "stream_access_denied" in response.text
    if protocol == "ag-ui":
        assert "RUN_ERROR" in response.text and "RUN_FINISHED" not in response.text
    with env.factory() as db:
        assert db.get(AgentRun, env.handle.run_id).status == "completed"


def test_current_grant_cannot_expand_initial_subscription_permission_ceiling(env):
    env.principal = replace(env.principal, scopes=("agent:invoke",))
    result = "".join(checked_stream(env))
    assert BODY not in result and "stream_access_denied" in result


def test_missing_required_permission_configuration_does_not_allow_reading(env):
    authorization = module().StreamAuthorization(
        session_factory=env.factory, principal=env.principal, session_id="s1",
        run_id=env.handle.run_id, permissions=(),
    )
    with pytest.raises(module().StreamAccessDenied):
        authorization.check()


def test_authorized_other_user_still_cannot_read_this_run(env):
    other_id = env.directory.grant(
        issuer=ISSUER,
        external_subject="other-user",
        tenant_id="tenant_1",
        permission_scopes=("agent:read",),
        actor_ref="test",
    )
    env.principal = env.provider.resolve_principal(tenant_id="tenant_1", user_id=other_id)
    assert "stream_access_denied" in "".join(checked_stream(env))


def test_jwt_subscription_without_verified_deadline_fails_closed(env, monkeypatch):
    monkeypatch.setattr(settings, "auth_provider", "jwt")
    result = "".join(checked_stream(env))
    assert BODY not in result and "stream_access_expired" in result


def test_authorization_database_failure_does_not_advance_source_or_leak_details(env):
    advanced = []

    def broken_factory():
        raise RuntimeError(CANARY)

    authorization = guard(env)
    authorization.session_factory = broken_factory

    def source():
        advanced.append(True)
        yield BODY

    result = "".join(module().authorized_event_stream(source(), authorization=authorization))
    assert "stream_access_unavailable" in result and CANARY not in result and not advanced


def test_delegated_approval_output_keeps_specific_owner_and_current_decide_scope(env):
    approver_id = env.directory.grant(
        issuer=ISSUER,
        external_subject="approver",
        tenant_id="tenant_1",
        permission_scopes=("agent:invoke", "approval:decide"),
        actor_ref="test",
    )
    approver = env.provider.resolve_principal(tenant_id="tenant_1", user_id=approver_id)
    authorization = module().StreamAuthorization(
        session_factory=env.factory,
        principal=approver,
        session_id="s1",
        run_id=env.handle.run_id,
        permissions=("agent:invoke", "approval:decide"),
        resource_owner_id=env.principal.user_id,
    )
    authorization.check()
    env.directory.grant(
        issuer=ISSUER,
        external_subject="approver",
        tenant_id="tenant_1",
        permission_scopes=("agent:invoke",),
        actor_ref="test",
    )
    with pytest.raises(module().StreamAccessDenied):
        authorization.check()


@pytest.mark.parametrize("expired", [False, True])
def test_fresh_protocol_post_keeps_request_expiry_after_background_delegation(
    env, monkeypatch, expired
):
    from time import time

    from app.api import runs
    from app.conversations import dispatcher

    env.directory.grant(
        issuer=ISSUER,
        external_subject="stream-user",
        tenant_id="tenant_1",
        permission_scopes=("agent:invoke",),
        actor_ref="test",
    )
    env.principal = replace(
        env.provider.resolve_principal(tenant_id="tenant_1", user_id=env.principal.user_id),
        expires_at=time() + 3600,
    )
    env.app.dependency_overrides[get_current_principal] = lambda: env.principal
    captured = []
    original = runs._response

    def expired_response(*args, **kwargs):
        captured.append(kwargs["principal"])
        if expired:
            monkeypatch.setattr(module(), "time", lambda: env.principal.expires_at)
        return original(*args, **kwargs)

    def completed_worker(*, session_factory, run_id, **kwargs):
        with session_factory() as db:
            durable = DurableRunService(db)
            owner = durable.claim(run_id)
            durable.append(
                run_id, owner, "messages", [{"type": "ai", "id": "a-new", "content": BODY}, {}]
            )
            ConversationService(db).complete_turn(tenant_id="tenant_1", run_id=run_id, content=BODY)
            durable.publish_status(run_id, owner)
            db.commit()

    monkeypatch.setattr(runs, "_response", expired_response)
    monkeypatch.setattr(dispatcher, "dispatch_turn", completed_worker)
    response = TestClient(env.app).post(
        "/api/sessions/s1/messages/protocol-stream",
        json={
            "input": {
                "messages": [{"type": "human", "content": "公开问题"}],
                "request_id": "new-expiring-post",
            },
        },
    )
    assert response.status_code == 200
    assert captured == [env.principal]
    if expired:
        assert BODY not in response.text and "stream_access_expired" in response.text
    else:
        assert BODY in response.text and "stream_access_" not in response.text


@pytest.mark.parametrize("revoke", ["before_decision", "before_body", "none"])
def test_ag_ui_approval_resume_binds_original_owner_and_checks_current_approver(
    env, monkeypatch, revoke
):
    from app.api import ag_ui, approvals
    from app.storage.models import ApprovalRecord

    owner_id = env.principal.user_id
    approver_id = env.directory.grant(
        issuer=ISSUER,
        external_subject="approver",
        tenant_id="tenant_1",
        permission_scopes=("agent:invoke", "approval:decide"),
        actor_ref="test",
    )
    approver = env.provider.resolve_principal(tenant_id="tenant_1", user_id=approver_id)
    env.app.dependency_overrides[get_current_principal] = lambda: approver
    with env.factory() as db:
        db.add(
            ApprovalRecord(
                approval_id="approval-stream",
                tenant_id="tenant_1",
                run_id=env.handle.run_id,
                approval_type="tool_call",
                subject_name="synthetic.action",
                risk_level="high",
                action_digest="d" * 64,
                checkpoint_thread_id="checkpoint-stream",
                checkpoint_namespace="",
                checkpoint_id="checkpoint-1",
                interrupt_id="interrupt-stream",
                requested_by=owner_id,
            )
        )
        db.commit()
    decisions = []

    def decide(**kwargs):
        decisions.append(kwargs)
        return {
            "run_id": env.handle.run_id,
            "run_status": "completed",
            "message_id": env.handle.assistant_message_id,
            "final_response": {"content": BODY, "trace_id": "trace-stream"},
        }

    def revoke_decide():
        env.directory.grant(
            issuer=ISSUER,
            external_subject="approver",
            tenant_id="tenant_1",
            permission_scopes=("agent:invoke",),
            actor_ref="test",
        )

    monkeypatch.setattr(approvals, "decide_approval", decide)
    if revoke == "before_decision":
        original = ag_ui._validate_resume_thread

        def validated_then_revoke(**kwargs):
            original(**kwargs)
            revoke_decide()

        monkeypatch.setattr(ag_ui, "_validate_resume_thread", validated_then_revoke)
    elif revoke == "before_body":
        original_encode = ag_ui._encode_events

        def encode_then_revoke(*args, **kwargs):
            for index, frame in enumerate(original_encode(*args, **kwargs)):
                if index == 1:
                    revoke_decide()
                yield frame

        monkeypatch.setattr(ag_ui, "_encode_events", encode_then_revoke)
    response = TestClient(env.app).post(
        "/api/ag-ui",
        json={
            "threadId": "s1",
            "runId": "approval-output",
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {},
            "resume": [
                {
                    "interruptId": "interrupt-stream",
                    "status": "resolved",
                    "payload": {"decision": "approved"},
                }
            ],
        },
    )
    if revoke == "before_decision":
        assert response.status_code == 403 and not decisions
    elif revoke == "before_body":
        assert response.status_code == 200 and len(decisions) == 1
        assert "stream_access_denied" in response.text and BODY not in response.text
    else:
        assert response.status_code == 200 and len(decisions) == 1
        assert BODY in response.text and "RUN_FINISHED" in response.text


def test_read_only_principal_can_replay_without_invoke_scope(env):
    env.directory.grant(
        issuer=ISSUER,
        external_subject="stream-user",
        tenant_id="tenant_1",
        permission_scopes=("agent:read",),
        actor_ref="test",
    )
    principal = env.provider.resolve_principal(tenant_id="tenant_1", user_id=env.principal.user_id)
    env.app.dependency_overrides[get_current_principal] = lambda: principal
    response = TestClient(env.app).get(f"/api/sessions/s1/runs/{env.handle.run_id}/events")
    assert response.status_code == 200 and BODY in response.text


def test_real_signed_jwt_deadline_survives_directory_mapping_to_live_subscription(env, monkeypatch):
    from time import time

    import jwt
    from app.api import runs
    from app.security import dependencies
    from app.security.jwt_provider import JwtPrincipalProvider
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    expiry = int(time()) + 3600
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": "bs-agent",
            "sub": "stream-user",
            "tenant_id": "tenant_1",
            "exp": expiry,
            "scope": "*",
            "roles": ["admin"],
        },
        key,
        algorithm="RS256",
    )
    selected = JwtPrincipalProvider(
        issuer=ISSUER, audience="bs-agent", allowed_algorithms=("RS256",), public_key=public
    )
    monkeypatch.setattr(settings, "auth_provider", "jwt")
    monkeypatch.setattr(dependencies, "get_jwt_principal_provider", lambda: selected)
    monkeypatch.setattr(dependencies, "get_rbac_provider", lambda: env.provider)
    env.app.dependency_overrides.pop(get_current_principal)
    original = runs._response

    def expired_after_auth(*args, **kwargs):
        assert kwargs["principal"].expires_at == expiry
        assert kwargs["principal"].user_id == env.principal.user_id
        monkeypatch.setattr(module(), "time", lambda: float(expiry))
        return original(*args, **kwargs)

    monkeypatch.setattr(runs, "_response", expired_after_auth)
    response = TestClient(env.app).get(
        f"/api/sessions/s1/runs/{env.handle.run_id}/events",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200 and "stream_access_expired" in response.text
    assert BODY not in response.text and token not in response.text
