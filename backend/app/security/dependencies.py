from collections.abc import Callable
from functools import lru_cache

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.operations.observability import bind_principal_log_context
from app.security.jwt_provider import (
    JwtAuthenticationError,
    JwtPrincipalProvider,
    build_jwks_client,
)
from app.security.principal import Principal
from app.security.rbac import get_rbac_provider


_bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def get_jwt_principal_provider() -> JwtPrincipalProvider:
    jwks_client = None
    if settings.jwt_jwks_url:
        jwks_client = build_jwks_client(
            url=settings.jwt_jwks_url,
            cache_ttl_seconds=settings.jwt_jwks_cache_ttl_seconds,
        )
    return JwtPrincipalProvider(
        issuer=settings.jwt_issuer or "",
        audience=settings.jwt_audience or "",
        allowed_algorithms=settings.jwt_allowed_algorithms,
        public_key=settings.jwt_public_key,
        jwks_client=jwks_client,
    )


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
) -> Principal:
    if settings.auth_provider == "mock":
        if not _has_text(x_tenant_id) or not _has_text(x_user_id):
            raise _authentication_required()
        principal = get_rbac_provider().resolve_principal(
            tenant_id=x_tenant_id.strip(),
            user_id=x_user_id.strip(),
        )
        bind_principal_log_context(principal)
        return principal

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _authentication_required()
    try:
        principal = get_jwt_principal_provider().authenticate(credentials.credentials)
        bind_principal_log_context(principal)
        return principal
    except JwtAuthenticationError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_token", "message": "身份令牌无效。"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_permission(permission: str) -> Callable[..., Principal]:
    def dependency(
        principal: Principal = Depends(get_current_principal),
    ) -> Principal:
        if "*" not in principal.scopes and permission not in principal.scopes:
            raise HTTPException(
                status_code=403,
                detail={"code": "permission_denied", "permission": permission},
            )
        return principal

    return dependency


def _authentication_required() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "authentication_required", "message": "需要身份认证。"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _has_text(value: str | None) -> bool:
    return value is not None and bool(value.strip())
