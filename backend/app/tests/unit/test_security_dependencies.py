import importlib.util
from importlib import import_module

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.security import dependencies
from app.security.dependencies import get_current_principal, require_permission
from app.security.principal import Principal


def test_security_dependencies_module_is_available():
    assert importlib.util.find_spec("app.security.dependencies") is not None


def test_security_dependencies_expose_principal_boundary():
    module = import_module("app.security.dependencies")

    assert callable(getattr(module, "get_current_principal", None))
    assert callable(getattr(module, "require_permission", None))


def _client():
    app = FastAPI()

    @app.get("/me")
    def me(principal: Principal = Depends(get_current_principal)):
        return {
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "scopes": principal.scopes,
        }

    @app.get("/approve")
    def approve(_: Principal = Depends(require_permission("approval:read"))):
        return {"ok": True}

    return TestClient(app)


def test_mock_identity_requires_explicit_headers(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "auth_provider", "mock")

    response = _client().get("/me")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "authentication_required"


def test_mock_identity_resolves_headers_through_rbac(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "auth_provider", "mock")
    monkeypatch.setattr(dependencies.settings, "mcp_server_name", "local")
    captured = []
    monkeypatch.setattr(dependencies, "bind_principal_log_context", captured.append)

    response = _client().get(
        "/me",
        headers={"X-Tenant-ID": "tenant-1", "X-User-ID": "user-1"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "scopes": [
            "agent:invoke",
            "asset:delete",
            "asset:read",
            "asset:write",
            "mcp:local",
                "rag:retrieve",
                "trace:read",
                "web:search",
            ],
    }
    assert captured[0].tenant_id == "tenant-1"
    assert captured[0].user_id == "user-1"


def test_permission_dependency_returns_403_without_required_scope(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "auth_provider", "mock")

    response = _client().get(
        "/approve",
        headers={"X-Tenant-ID": "tenant-1", "X-User-ID": "user-1"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "permission_denied",
        "permission": "approval:read",
    }


def test_permission_dependency_accepts_configured_mock_approver(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "auth_provider", "mock")

    response = _client().get(
        "/approve",
        headers={"X-Tenant-ID": "tenant-1", "X-User-ID": "approver_1"},
    )

    assert response.status_code == 200


def test_jwt_identity_uses_bearer_token_and_provider(monkeypatch):
    captured = []

    class FakeProvider:
        def authenticate(self, token):
            captured.append(token)
            return Principal(
                tenant_id="tenant-jwt",
                user_id="user-jwt",
                scopes=("agent:invoke",),
                token_id="token-jwt",
            )

    monkeypatch.setattr(dependencies.settings, "auth_provider", "jwt")
    monkeypatch.setattr(
        dependencies,
        "get_jwt_principal_provider",
        lambda: FakeProvider(),
    )

    response = _client().get(
        "/me",
        headers={"Authorization": "Bearer signed-token"},
    )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant-jwt"
    assert captured == ["signed-token"]
