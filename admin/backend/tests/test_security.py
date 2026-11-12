"""Admin 真实本地签名与权限目录测试，不访问外部 IdP。"""

from datetime import UTC, datetime, timedelta
from importlib import import_module

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import create_app

ISSUER = "https://identity.example.test"


def security():
    try:
        return import_module("app.security")
    except ModuleNotFoundError:
        pytest.fail("Admin 鉴权尚未实现", pytrace=False)


@pytest.fixture
def env():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = security().SecuritySettings(
        mode="jwt",
        issuer=ISSUER,
        user_audience="admin-user",
        service_audience="admin-runtime",
        public_key=key.public_key(),
    )
    app = create_app(security_settings=config)

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    yield TestClient(app), factory, key
    engine.dispose()


def token(env, subject="user-a", audience="admin-user", **overrides):
    claims = {
        "iss": ISSUER,
        "sub": subject,
        "aud": audience,
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "roles": ["admin"],
        "scope": "*",
    }
    claims.update(overrides)
    return {"Authorization": "Bearer " + jwt.encode(claims, env[2], algorithm="RS256")}


def grant(env, kind="user", subject="user-a", permissions=None):
    with env[1]() as db:
        security().set_grant(
            db,
            issuer=ISSUER,
            subject=subject,
            kind=kind,
            permissions=permissions
            or (
                ["config:read", "config:write"]
                if kind == "user"
                else ["runtime:read", "connection:read"]
            ),
            actor_ref="test",
        )
        db.commit()


@pytest.mark.parametrize(
    "path",
    [
        "/api/business-intents",
        "/api/skills",
        "/api/market-data-providers",
        "/api/internal/market-data-providers/active/mcp-connection",
        "/api/runtime/skills",
    ],
)
def test_anonymous_config_and_internal_access_denied(env, path):
    assert env[0].get(path).status_code == 401


def test_token_roles_do_not_grant_admin(env):
    assert env[0].get("/api/skills", headers=token(env)).status_code == 403


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "main-api"},
        {"iss": "https://evil.test"},
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
        {"sub": " user-a "},
    ],
)
def test_invalid_token_is_rejected(env, overrides):
    grant(env)
    assert env[0].get("/api/skills", headers=token(env, **overrides)).status_code == 401


def test_user_can_configure_but_cannot_read_full_connection(env):
    grant(env)
    assert env[0].get("/api/skills", headers=token(env)).status_code == 200
    assert (
        env[0]
        .get("/api/internal/market-data-providers/active/mcp-connection", headers=token(env))
        .status_code
        == 401
    )


def test_service_has_runtime_read_but_no_config_permissions(env):
    grant(env, kind="service", subject="runtime")
    headers = token(env, subject="runtime", audience="admin-runtime")
    assert env[0].get("/api/runtime/skills", headers=headers).status_code == 200
    assert env[0].get("/api/skills", headers=headers).status_code == 401
    assert env[0].post("/api/skills", json={}, headers=headers).status_code == 401


def test_read_only_user_cannot_mutate_and_revocation_is_immediate(env):
    grant(env, permissions=["config:read"])
    headers = token(env)
    assert env[0].get("/api/skills", headers=headers).status_code == 200
    assert env[0].post("/api/skills", json={}, headers=headers).status_code == 403
    with env[1]() as db:
        security().revoke_grant(db, issuer=ISSUER, subject="user-a", kind="user", actor_ref="test")
        db.commit()
    assert env[0].get("/api/skills", headers=headers).status_code == 403


def test_service_reads_full_connection_while_user_response_stays_redacted(env):
    grant(env)
    payload = {
        "id": "tushare-security",
        "provider": "tushare",
        "display_name": "测试数据源",
        "enabled": True,
        "active": True,
        "settings": {"mcp_server_url": "https://api.tushare.pro/mcp/token=synthetic-secret"},
    }
    response = env[0].post("/api/market-data-providers", json=payload, headers=token(env))
    assert response.status_code == 201, response.text
    assert "synthetic-secret" not in response.text
    grant(env, kind="service", subject="runtime")
    response = env[0].get(
        "/api/internal/market-data-providers/active/mcp-connection",
        headers=token(env, subject="runtime", audience="admin-runtime"),
    )
    assert response.status_code == 200
    assert response.json()["server_url"].endswith("synthetic-secret")


def test_same_subject_in_user_directory_is_not_a_service(env):
    grant(env)
    response = env[0].get("/api/runtime/skills", headers=token(env, audience="admin-runtime"))
    assert response.status_code == 403


def test_directory_failure_is_safe_503(env):
    from app.models import SecurityGrantRecord

    grant(env)
    SecurityGrantRecord.__table__.drop(env[1].kw["bind"])
    response = env[0].get("/api/skills", headers=token(env))
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "admin_authorization_unavailable"}


def test_audit_failure_rolls_back_grant(env):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError

    from app.models import SecurityGrantEventRecord, SecurityGrantRecord

    grant(env)
    engine = env[1].kw["bind"]

    def fail(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO admin_security_grant_events"):
            raise SQLAlchemyError("simulated audit failure")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with env[1]() as db, pytest.raises(SQLAlchemyError), db.begin():
            security().revoke_grant(
                db, issuer=ISSUER, subject="user-a", kind="user", actor_ref="test"
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with env[1]() as db:
        assert db.get(SecurityGrantRecord, (ISSUER, "user-a", "user")).enabled
        assert db.query(SecurityGrantEventRecord).count() == 1


def test_settings_forbid_production_mock_and_shared_audience():
    with pytest.raises(ValueError):
        security().SecuritySettings(mode="mock", environment="production")
    with pytest.raises(ValueError):
        security().SecuritySettings(
            issuer=ISSUER, user_audience="same", service_audience="same", public_key="key"
        )


def test_main_service_adapter_to_admin_with_real_signed_service_token(env):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    import httpx

    spec = spec_from_file_location(
        "runtime_auth_adapter",
        Path(__file__).resolve().parents[3] / "backend/app/security/admin_service_auth.py",
    )
    adapter = module_from_spec(spec)
    spec.loader.exec_module(adapter)
    grant(env, kind="service", subject="runtime")
    signed = token(env, subject="runtime", audience="admin-runtime")["Authorization"].split(" ", 1)[
        1
    ]
    token_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"access_token": signed, "token_type": "Bearer", "expires_in": 120}
            )
        )
    )
    auth = adapter.AdminServiceAuth(
        token_url="https://idp.test/token",
        client_id="runtime",
        client_secret="synthetic-client-secret",
        scope="",
        allowed_base_urls=["https://admin.test"],
        token_client=token_client,
    )

    def call_admin(request):
        response = env[0].get(request.url.raw_path.decode(), headers=dict(request.headers))
        return httpx.Response(response.status_code, json=response.json())

    with httpx.Client(auth=auth, transport=httpx.MockTransport(call_admin)) as client:
        assert client.get("https://admin.test/api/runtime/skills").status_code == 200
        with env[1]() as db:
            security().revoke_grant(
                db, issuer=ISSUER, subject="runtime", kind="service", actor_ref="test"
            )
            db.commit()
        assert client.get("https://admin.test/api/runtime/skills").status_code == 403


@pytest.mark.parametrize("algorithm", ["HS256", "none"])
def test_symmetric_and_unsigned_tokens_are_rejected(env, algorithm):
    grant(env)
    claims = {
        "iss": ISSUER,
        "sub": "user-a",
        "aud": "admin-user",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    signed = jwt.encode(
        claims,
        "synthetic-secret-at-least-thirty-two-bytes" if algorithm == "HS256" else None,
        algorithm=algorithm,
    )
    response = env[0].get("/api/skills", headers={"Authorization": "Bearer " + signed})
    assert response.status_code == 401


def test_cli_requires_explicit_database(monkeypatch):
    from app.security_cli import main

    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "grant",
                "--issuer",
                ISSUER,
                "--subject",
                "user-a",
                "--kind",
                "user",
                "--permission",
                "config:read",
            ]
        )
    assert exc.value.code == 2


def test_cli_grant_revoke_audits_os_identity(tmp_path, monkeypatch):
    from sqlalchemy import select

    from app.models import SecurityGrantEventRecord, SecurityGrantRecord
    from app.security_cli import main

    url = f"sqlite:///{tmp_path / 'admin-cli.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ADMIN_DATABASE_URL", url)
    arguments = ["--issuer", ISSUER, "--subject", "runtime", "--kind", "service"]
    assert main(["grant", *arguments, "--permission", "runtime:read"]) == 0
    assert main(["revoke", *arguments]) == 0
    with sessionmaker(bind=engine)() as db:
        assert not db.get(SecurityGrantRecord, (ISSUER, "runtime", "service")).enabled
        events = list(db.scalars(select(SecurityGrantEventRecord)))
        assert len(events) == 2
        assert all(event.actor_ref.startswith("local-uid:") for event in events)
    engine.dispose()
