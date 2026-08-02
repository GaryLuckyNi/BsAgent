from functools import lru_cache
from typing import Protocol

from app.core.config import settings
from app.security.principal import Principal


BASE_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "analyst": frozenset(
        {
            "agent:invoke",
            "rag:retrieve",
            "asset:write",
            "asset:read",
            "asset:delete",
            "trace:read",
            "web:search",
        }
    ),
    "approver": frozenset(
        {
            "agent:invoke",
            "rag:retrieve",
            "approval:read",
            "approval:decide",
            "asset:write",
            "asset:read",
            "asset:delete",
            "trace:read",
            "web:search",
        }
    ),
    "admin": frozenset({"*"}),
}


class RbacProvider(Protocol):
    def resolve_principal(self, *, tenant_id: str, user_id: str) -> Principal: ...

    def has_permission(self, principal: Principal, permission: str) -> bool: ...

    def tool_scopes(self, principal: Principal) -> tuple[str, ...]: ...


class MockRbacProvider:
    def __init__(
        self,
        *,
        approver_user_ids: list[str] | tuple[str, ...] | None = None,
        admin_user_ids: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self._approver_user_ids = frozenset(approver_user_ids or [])
        self._admin_user_ids = frozenset(admin_user_ids or [])

    def resolve_principal(self, *, tenant_id: str, user_id: str) -> Principal:
        if user_id in self._admin_user_ids:
            roles = ("admin",)
        elif user_id in self._approver_user_ids:
            roles = ("approver",)
        else:
            roles = ("analyst",)
        permissions = tuple(
            sorted(
                {
                    permission
                    for role in roles
                    for permission in _role_permissions(role)
                }
            )
        )
        return Principal(
            tenant_id=tenant_id,
            user_id=user_id,
            scopes=permissions,
            roles=roles,
        )

    def has_permission(self, principal: Principal, permission: str) -> bool:
        return "*" in principal.permissions or permission in principal.permissions

    def tool_scopes(self, principal: Principal) -> tuple[str, ...]:
        return principal.permissions


@lru_cache(maxsize=1)
def get_rbac_provider() -> RbacProvider:
    return MockRbacProvider(
        approver_user_ids=settings.mock_rbac_approver_user_ids,
        admin_user_ids=settings.mock_rbac_admin_user_ids,
    )


def _role_permissions(role: str) -> frozenset[str]:
    permissions = set(BASE_ROLE_PERMISSIONS[role])
    if role in {"analyst", "approver"}:
        mcp_scope = _configured_mcp_scope()
        if mcp_scope:
            permissions.add(mcp_scope)
    return frozenset(permissions)


def _configured_mcp_scope() -> str | None:
    server_name = settings.mcp_server_name.strip()
    if not server_name:
        return None
    return f"mcp:{server_name}"
