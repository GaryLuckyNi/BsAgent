"""复用 OIDC 签发的 JWT，业务授权取当前目录；用户与服务用途严格分开。"""

import os
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import urlparse

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.authorization import PERMISSIONS, revoke_grant, set_grant  # noqa: F401
from app.database import get_db
from app.models import SecurityGrantRecord

_bearer = HTTPBearer(auto_error=False)
_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)


@dataclass(frozen=True)
class SecuritySettings:
    mode: str = "jwt"
    environment: str = "production"
    issuer: str = ""
    user_audience: str = ""
    service_audience: str = ""
    public_key: Any = field(default=None, repr=False)
    jwks_url: str = ""
    algorithms: tuple[str, ...] = ("RS256",)
    jwks: Any = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if self.mode == "mock" and self.environment in {"local", "test"}:
            return
        if self.mode != "jwt" or not self.algorithms or not set(self.algorithms) <= _ALGORITHMS:
            raise ValueError("Admin 必须使用 JWT；mock 仅限显式 local/test 环境。")
        for value in (self.issuer, self.user_audience, self.service_audience):
            if not value or value != value.strip() or len(value) > 256:
                raise ValueError("Admin JWT issuer/audience 配置无效。")
        if self.user_audience == self.service_audience:
            raise ValueError("Admin 用户与服务 audience 必须不同。")
        if (self.public_key is None) == (not self.jwks_url):
            raise ValueError("Admin 必须且只能配置公钥或 JWKS URL。")
        if self.jwks_url:
            parsed = urlparse(self.jwks_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError("Admin JWKS 必须使用受信 HTTPS 地址。")
            object.__setattr__(
                self, "jwks", jwt.PyJWKClient(self.jwks_url, lifespan=300, timeout=5)
            )

    @classmethod
    def from_environment(cls):
        return cls(
            mode=os.getenv("ADMIN_AUTH_MODE", "jwt"),
            environment=os.getenv("ADMIN_APP_ENV", "production"),
            issuer=os.getenv("ADMIN_JWT_ISSUER", ""),
            user_audience=os.getenv("ADMIN_JWT_USER_AUDIENCE", ""),
            service_audience=os.getenv("ADMIN_JWT_SERVICE_AUDIENCE", ""),
            public_key=os.getenv("ADMIN_JWT_PUBLIC_KEY") or None,
            jwks_url=os.getenv("ADMIN_JWT_JWKS_URL", ""),
            algorithms=tuple(os.getenv("ADMIN_JWT_ALGORITHMS", "RS256").split(",")),
        )


def _unauthenticated():
    return HTTPException(
        status_code=401,
        detail={"code": "admin_invalid_token"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authenticate(config, token, kind):
    try:
        if (
            len(token) > 32768
            or jwt.get_unverified_header(token).get("alg") not in config.algorithms
        ):
            raise ValueError("invalid algorithm")
        key = (
            config.public_key
            if config.public_key is not None
            else config.jwks.get_signing_key_from_jwt(token).key
        )
        claims = jwt.decode(
            token,
            key=key,
            algorithms=list(config.algorithms),
            issuer=config.issuer,
            audience=config.user_audience if kind == "user" else config.service_audience,
            options={"require": ["iss", "aud", "exp", "sub"], "strict_aud": True},
        )
        subject = claims["sub"]
        if (
            not isinstance(subject, str)
            or not subject
            or subject != subject.strip()
            or len(subject) > 256
        ):
            raise ValueError("invalid subject")
        return subject
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise _unauthenticated() from exc


def require_access(kind, permission):
    def dependency(
        request: Request,
        db: Annotated[Session, Depends(get_db)],
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    ):
        config = request.app.state.security_settings
        if config.mode == "mock":
            return {"kind": kind, "permissions": sorted(PERMISSIONS[kind]), "mock": True}
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise _unauthenticated()
        subject = _authenticate(config, credentials.credentials, kind)
        try:
            row = db.get(
                SecurityGrantRecord, (config.issuer, subject, kind), populate_existing=True
            )
            needed = permission(request) if callable(permission) else permission
            if (
                row is None
                or not row.enabled
                or not isinstance(row.permissions, list)
                or any(
                    not isinstance(p, str) or p not in PERMISSIONS[kind] for p in row.permissions
                )
                or needed not in row.permissions
            ):
                raise HTTPException(status_code=403, detail={"code": "admin_permission_denied"})
            return {
                "kind": kind,
                "permissions": list(row.permissions),
                "subject": subject,
                "mock": False,
            }
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(
                status_code=503, detail={"code": "admin_authorization_unavailable"}
            ) from exc

    return dependency


config_access = require_access(
    "user", lambda request: "config:read" if request.method in {"GET", "HEAD"} else "config:write"
)
runtime_access = require_access("service", "runtime:read")
connection_access = require_access("service", "connection:read")
