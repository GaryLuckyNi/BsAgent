from app.security import rbac
from app.security.rbac import MockRbacProvider


def test_mock_rbac_assigns_analyst_permissions_to_regular_user(monkeypatch):
    monkeypatch.setattr(rbac.settings, "mcp_server_name", "local")
    provider = MockRbacProvider()

    principal = provider.resolve_principal(tenant_id="tenant_1", user_id="user_1")

    assert principal.roles == ("analyst",)
    assert provider.has_permission(principal, "agent:invoke") is True
    assert provider.has_permission(principal, "agent:read") is True
    assert provider.has_permission(principal, "asset:write") is True
    assert provider.has_permission(principal, "asset:read") is True
    assert provider.has_permission(principal, "trace:read") is True
    assert provider.has_permission(principal, "mcp:local") is True
    assert provider.has_permission(principal, "approval:decide") is False


def test_mock_rbac_uses_configured_mcp_server_scope(monkeypatch):
    monkeypatch.setattr(rbac.settings, "mcp_server_name", "tushare")
    provider = MockRbacProvider()

    principal = provider.resolve_principal(tenant_id="tenant_1", user_id="user_1")

    assert provider.has_permission(principal, "mcp:tushare") is True
    assert provider.has_permission(principal, "mcp:local") is False


def test_mock_rbac_assigns_approval_permission_to_configured_approver():
    provider = MockRbacProvider(approver_user_ids=["approver_1"])

    principal = provider.resolve_principal(
        tenant_id="tenant_1",
        user_id="approver_1",
    )

    assert principal.roles == ("approver",)
    assert provider.has_permission(principal, "approval:read") is True
    assert provider.has_permission(principal, "approval:decide") is True


def test_mock_rbac_admin_wildcard_grants_any_permission():
    provider = MockRbacProvider(admin_user_ids=["admin_1"])

    principal = provider.resolve_principal(tenant_id="tenant_1", user_id="admin_1")

    assert provider.has_permission(principal, "mcp:any-server") is True
    assert provider.has_permission(principal, "approval:decide") is True
