"""真实权限源使用独立 SQLite；不访问外部身份服务。"""

from importlib import import_module

import pytest
from app.core.config import settings
from app.security import rbac
from app.security.principal import Principal
from app.storage.models import Base
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ISSUER = "https://identity.example.test/realms/research"


@pytest.fixture
def directory_module():
    try:
        return import_module("app.security.directory")
    except ModuleNotFoundError as exc:
        if exc.name != "app.security.directory":
            raise
        pytest.fail("真实权限目录尚未实现", pytrace=False)


@pytest.fixture
def directory(directory_module):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield directory_module.PermissionDirectory(factory)
    engine.dispose()


def grant(directory, **overrides):
    values = {
        "issuer": ISSUER,
        "external_subject": "external-a1",
        "tenant_id": "tenant-a",
        "roles": ("analyst",),
        "permission_scopes": (),
        "actor_ref": "test-operator",
    }
    values.update(overrides)
    return directory.grant(**values)


def token_identity(**overrides):
    values = {
        "tenant_id": "tenant-a",
        "user_id": "external-a1",
        "scopes": ("*",),
        "roles": ("admin",),
        "token_id": "test-jti",
    }
    values.update(overrides)
    return Principal(**values)


def test_authenticated_mapping_keeps_verified_token_deadline(directory, directory_module):
    grant(directory)
    identity = token_identity(expires_at=2_000_000_000.0)
    resolved = provider(directory, directory_module).resolve_authenticated_principal(identity)
    assert resolved.expires_at == identity.expires_at


def provider(directory, directory_module, issuer=ISSUER):
    return directory_module.DirectoryRbacProvider(directory, issuer=issuer)


def test_principal_backend_does_not_return_mock(monkeypatch):
    monkeypatch.setattr(settings, "rbac_backend", "principal")
    monkeypatch.setattr(settings, "jwt_issuer", ISSUER)
    rbac.get_rbac_provider.cache_clear()
    try:
        assert type(rbac.get_rbac_provider()).__name__ == "DirectoryRbacProvider"
    finally:
        rbac.get_rbac_provider.cache_clear()


def test_directory_binds_issuer_and_subject_to_stable_internal_id(directory, directory_module):
    user_id = grant(directory)
    assert user_id != "external-a1"
    assert grant(directory) == user_id
    principal = provider(directory, directory_module).resolve_authenticated_principal(
        token_identity()
    )
    assert principal.user_id == user_id
    assert principal.tenant_id == "tenant-a"
    assert principal.token_id == "test-jti"
    assert principal.roles == ("analyst",)
    assert "agent:invoke" in principal.scopes
    assert "*" not in principal.scopes
    assert "approval:decide" not in principal.scopes


@pytest.mark.parametrize(
    "identity", [token_identity(user_id="unknown"), token_identity(tenant_id="tenant-b")]
)
def test_unknown_subject_or_other_tenant_is_denied(directory, directory_module, identity):
    grant(directory)
    with pytest.raises(directory_module.PermissionDirectoryDenied):
        provider(directory, directory_module).resolve_authenticated_principal(identity)


def test_issuer_change_cannot_rebind_existing_account(directory, directory_module):
    user_id = grant(directory)
    other = provider(directory, directory_module, issuer="https://other.example.test")
    with pytest.raises(directory_module.PermissionDirectoryDenied):
        other.resolve_authenticated_principal(token_identity())
    with pytest.raises(directory_module.PermissionDirectoryDenied):
        other.resolve_principal(tenant_id="tenant-a", user_id=user_id)


def test_same_subject_from_different_issuers_has_different_internal_id(directory):
    assert grant(directory) != grant(directory, issuer="https://other.example.test")


def test_empty_grants_do_not_default_to_analyst(directory, directory_module):
    grant(directory, roles=())
    with pytest.raises(directory_module.PermissionDirectoryDenied):
        provider(directory, directory_module).resolve_authenticated_principal(token_identity())


def test_explicit_scopes_and_approver_role_are_resolved_from_directory(directory, directory_module):
    grant(directory, roles=("approver",), permission_scopes=("mcp:research",))
    principal = provider(directory, directory_module).resolve_authenticated_principal(
        token_identity()
    )
    assert "approval:decide" in principal.scopes
    assert "mcp:research" in principal.scopes


@pytest.mark.parametrize(
    "values",
    [
        {"roles": ("admin",)},
        {"roles": ("unknown",)},
        {"permission_scopes": ("*",)},
        {"permission_scopes": ("mcp:*",)},
    ],
)
def test_unbounded_or_unknown_grants_are_rejected_without_audit_write(directory, values):
    from app.storage.models import SecurityDirectoryEventRecord

    with pytest.raises(ValueError):
        grant(directory, **values)
    with directory.session_factory() as session:
        assert list(session.scalars(select(SecurityDirectoryEventRecord))) == []


def test_revoke_is_observed_by_approval_resume_and_old_principal(directory, directory_module):
    user_id = grant(directory, roles=("approver",))
    selected = provider(directory, directory_module)
    previous = selected.resolve_principal(tenant_id="tenant-a", user_id=user_id)
    directory.revoke_membership(user_id=user_id, tenant_id="tenant-a", actor_ref="test-operator")
    with pytest.raises(directory_module.PermissionDirectoryDenied):
        selected.resolve_principal(tenant_id="tenant-a", user_id=user_id)
    with pytest.raises(directory_module.PermissionDirectoryDenied):
        selected.tool_scopes(previous)
    assert selected.has_permission(previous, "approval:decide") is False


def test_disabled_subject_not_implicitly_reenabled_by_grant(directory, directory_module):
    grant(directory)
    directory.set_subject_enabled(
        issuer=ISSUER, external_subject="external-a1", enabled=False, actor_ref="test-operator"
    )
    grant(directory, roles=("approver",))
    with pytest.raises(directory_module.PermissionDirectoryDenied):
        provider(directory, directory_module).resolve_authenticated_principal(token_identity())
    directory.set_subject_enabled(
        issuer=ISSUER, external_subject="external-a1", enabled=True, actor_ref="test-operator"
    )
    assert (
        "approval:decide"
        in provider(directory, directory_module)
        .resolve_authenticated_principal(token_identity())
        .scopes
    )


def test_role_change_is_visible_without_restarting_provider(directory, directory_module):
    user_id = grant(directory, roles=("approver",))
    selected = provider(directory, directory_module)
    assert (
        "approval:decide"
        in selected.resolve_principal(tenant_id="tenant-a", user_id=user_id).scopes
    )
    grant(directory, roles=("analyst",))
    assert (
        "approval:decide"
        not in selected.resolve_principal(tenant_id="tenant-a", user_id=user_id).scopes
    )


def test_directory_unavailable_raises_safe_error(directory_module):
    def broken_factory():
        raise RuntimeError("postgresql://secret-password@private/db")

    selected = provider(directory_module.PermissionDirectory(broken_factory), directory_module)
    with pytest.raises(directory_module.PermissionDirectoryUnavailable) as error:
        selected.resolve_authenticated_principal(token_identity())
    assert "secret-password" not in str(error.value)


def test_changes_are_audited_in_same_transaction(directory, directory_module):
    from app.storage.models import SecurityDirectoryEventRecord

    user_id = grant(directory)
    directory.revoke_membership(user_id=user_id, tenant_id="tenant-a", actor_ref="test-operator")
    with directory.session_factory() as session:
        events = list(session.scalars(select(SecurityDirectoryEventRecord)))
    assert {item.action for item in events} == {"grant", "revoke_membership"}
    assert all(item.actor_ref == "test-operator" and item.user_id == user_id for item in events)


def authenticated_client(monkeypatch, selected_provider, identity=None):
    from typing import Annotated

    from app.security import dependencies
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    class TokenProvider:
        def authenticate(self, _token):
            return identity or token_identity()

    monkeypatch.setattr(settings, "auth_provider", "jwt")
    monkeypatch.setattr(settings, "rbac_backend", "principal")
    monkeypatch.setattr(dependencies, "get_jwt_principal_provider", TokenProvider)
    monkeypatch.setattr(dependencies, "get_rbac_provider", lambda: selected_provider)
    app = FastAPI()

    @app.get("/protected")
    def protected(
        principal: Annotated[Principal, Depends(dependencies.require_permission("agent:read"))],
    ):
        return {"user_id": principal.user_id, "scopes": principal.scopes}

    @app.get("/approve")
    def approve(
        _principal: Annotated[
            Principal, Depends(dependencies.require_permission("approval:decide"))
        ],
    ):
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_jwt_authentication_uses_directory_instead_of_token_wildcard(
    directory,
    directory_module,
    monkeypatch,
):
    user_id = grant(directory)
    client = authenticated_client(monkeypatch, provider(directory, directory_module))
    headers = {
        "Authorization": "Bearer synthetic-token",
        "X-Tenant-ID": "tenant-b",
        "X-User-ID": "pretend-admin",
    }
    response = client.get("/protected", headers=headers)
    assert response.status_code == 200
    assert response.json()["user_id"] == user_id
    assert "*" not in response.json()["scopes"]
    assert client.get("/approve", headers=headers).status_code == 403


def test_authenticated_unknown_user_is_not_automatically_provisioned(
    directory,
    directory_module,
    monkeypatch,
):
    client = authenticated_client(monkeypatch, provider(directory, directory_module))
    assert client.get("/protected").status_code == 401
    response = client.get("/protected", headers={"Authorization": "Bearer synthetic-token"})
    assert response.status_code == 403
    from app.storage.models import SecuritySubjectRecord

    with directory.session_factory() as session:
        assert list(session.scalars(select(SecuritySubjectRecord))) == []


def test_next_http_request_observes_revocation(directory, directory_module, monkeypatch):
    user_id = grant(directory)
    client = authenticated_client(monkeypatch, provider(directory, directory_module))
    headers = {"Authorization": "Bearer synthetic-token"}
    assert client.get("/protected", headers=headers).status_code == 200
    directory.revoke_membership(user_id=user_id, tenant_id="tenant-a", actor_ref="test-operator")
    assert client.get("/protected", headers=headers).status_code == 403


def test_http_directory_failure_is_safe_503(directory_module, monkeypatch):
    def broken_factory():
        raise RuntimeError("postgresql://secret-password@private/db")

    selected = provider(directory_module.PermissionDirectory(broken_factory), directory_module)
    client = authenticated_client(monkeypatch, selected)
    response = client.get("/protected", headers={"Authorization": "Bearer synthetic-token"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "permission_directory_unavailable"
    assert "secret-password" not in response.text


def test_approval_resume_uses_current_internal_user_binding(
    directory, directory_module, monkeypatch
):
    from types import SimpleNamespace

    from app.api import approvals
    from fastapi import HTTPException

    user_id = grant(directory)
    monkeypatch.setattr(
        approvals, "get_rbac_provider", lambda: provider(directory, directory_module)
    )
    record = SimpleNamespace(tenant_id="tenant-a", requested_by=user_id)
    assert "agent:invoke" in approvals._resolve_resume_permission_scopes(record)
    directory.revoke_membership(user_id=user_id, tenant_id="tenant-a", actor_ref="test-operator")
    with pytest.raises(HTTPException) as error:
        approvals._resolve_resume_permission_scopes(record)
    assert error.value.status_code == 403


def test_cli_provisions_revokes_and_preserves_disabled_state(directory, directory_module, capsys):
    try:
        cli = import_module("app.security.directory_cli")
    except ModuleNotFoundError:
        pytest.fail("受控权限管理命令尚未实现", pytrace=False)
    assert (
        cli.main(
            [
                "grant",
                "--issuer",
                ISSUER,
                "--subject",
                "external-a1",
                "--tenant",
                "tenant-a",
                "--role",
                "analyst",
            ],
            session_factory=directory.session_factory,
        )
        == 0
    )
    import json

    user_id = json.loads(capsys.readouterr().out)["user_id"]
    assert (
        cli.main(
            ["revoke", "--user-id", user_id, "--tenant", "tenant-a"],
            session_factory=directory.session_factory,
        )
        == 0
    )
    with pytest.raises(directory_module.PermissionDirectoryDenied):
        provider(directory, directory_module).resolve_principal(
            tenant_id="tenant-a", user_id=user_id
        )
    assert (
        cli.main(
            ["disable-subject", "--issuer", ISSUER, "--subject", "external-a1"],
            session_factory=directory.session_factory,
        )
        == 0
    )
    grant(directory)
    with pytest.raises(directory_module.PermissionDirectoryDenied):
        provider(directory, directory_module).resolve_authenticated_principal(token_identity())
    assert (
        cli.main(
            ["enable-subject", "--issuer", ISSUER, "--subject", "external-a1"],
            session_factory=directory.session_factory,
        )
        == 0
    )
    assert provider(directory, directory_module).resolve_authenticated_principal(token_identity())


def test_cli_rejects_wildcard_without_persisting_subject(directory, directory_module, capsys):
    try:
        cli = import_module("app.security.directory_cli")
    except ModuleNotFoundError:
        pytest.fail("受控权限管理命令尚未实现", pytrace=False)
    assert (
        cli.main(
            [
                "grant",
                "--issuer",
                ISSUER,
                "--subject",
                "external-a1",
                "--tenant",
                "tenant-a",
                "--scope",
                "*",
            ],
            session_factory=directory.session_factory,
        )
        == 2
    )
    assert "通配符" in capsys.readouterr().err
    from app.storage.models import SecuritySubjectRecord

    with directory.session_factory() as session:
        assert list(session.scalars(select(SecuritySubjectRecord))) == []


def test_directory_event_failure_rolls_back_grant(directory, directory_module):
    from app.storage.models import SecurityDirectoryEventRecord, SecuritySubjectRecord
    from sqlalchemy import event

    def fail_audit(_mapper, _connection, _target):
        from sqlalchemy.exc import SQLAlchemyError

        raise SQLAlchemyError("synthetic-audit-failure")

    event.listen(SecurityDirectoryEventRecord, "before_insert", fail_audit)
    try:
        with pytest.raises(directory_module.PermissionDirectoryUnavailable):
            grant(directory)
    finally:
        event.remove(SecurityDirectoryEventRecord, "before_insert", fail_audit)
    with directory.session_factory() as session:
        assert list(session.scalars(select(SecuritySubjectRecord))) == []


def test_directory_migration_upgrade_and_downgrade(tmp_path):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    source = (
        Path(__file__).resolve().parents[4]
        / "alembic/versions/20261102_0017_permission_directory.py"
    )
    spec = spec_from_file_location("permission_directory_migration", source)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'directory-migration.sqlite'}")
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert set(inspect(connection).get_table_names()) == {
            "security_subjects",
            "security_memberships",
            "security_directory_events",
        }
        assert inspect(connection).get_unique_constraints("security_subjects")[0][
            "column_names"
        ] == [
            "issuer",
            "external_subject",
        ]
        migration.downgrade()
        assert inspect(connection).get_table_names() == []
    engine.dispose()


def test_cli_requires_explicit_database_configuration(monkeypatch, capsys, tmp_path):
    from app.security.directory_cli import main

    monkeypatch.delenv("BS_AGENT_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        main(
            [
                "grant",
                "--issuer",
                ISSUER,
                "--subject",
                "external-a1",
                "--tenant",
                "tenant-a",
                "--role",
                "analyst",
            ]
        )
    assert error.value.code == 2
    assert "BS_AGENT_DATABASE_URL" in capsys.readouterr().err


def test_corrupt_grants_do_not_fall_back_to_a_role(directory, directory_module):
    from app.storage.models import SecurityMembershipRecord

    user_id = grant(directory)
    with directory.session_factory() as session, session.begin():
        member = session.get(SecurityMembershipRecord, ("tenant-a", user_id))
        member.roles_json = {"admin": True}
    with pytest.raises(directory_module.PermissionDirectoryDenied):
        provider(directory, directory_module).resolve_authenticated_principal(token_identity())


@pytest.mark.parametrize("changed_claim", [{"sub": " external-a1 "}, {"tenant_id": " tenant-a "}])
def test_real_signed_jwt_cannot_normalize_into_another_identity(
    directory,
    directory_module,
    monkeypatch,
    changed_claim,
):
    from datetime import UTC, datetime, timedelta

    import jwt
    from app.security import dependencies
    from app.security.jwt_provider import JwtPrincipalProvider
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = JwtPrincipalProvider(
        issuer=ISSUER,
        audience="bs-agent",
        allowed_algorithms=("RS256",),
        public_key=key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )
    user_id = grant(directory, roles=("approver",))
    client = authenticated_client(monkeypatch, provider(directory, directory_module))
    monkeypatch.setattr(dependencies, "get_jwt_principal_provider", lambda: verifier)
    claims = {
        "iss": ISSUER,
        "sub": "external-a1",
        "tenant_id": "tenant-a",
        "aud": "bs-agent",
        "exp": datetime.now(UTC) + timedelta(minutes=2),
        "scope": "openid profile",
    }
    valid = jwt.encode(claims, key, algorithm="RS256")
    normal = client.get("/protected", headers={"Authorization": f"Bearer {valid}"})
    assert normal.status_code == 200 and normal.json()["user_id"] == user_id
    altered = jwt.encode({**claims, **changed_claim}, key, algorithm="RS256")
    response = client.get("/approve", headers={"Authorization": f"Bearer {altered}"})
    assert response.status_code == 401
