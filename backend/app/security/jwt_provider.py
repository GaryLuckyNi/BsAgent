from math import isfinite
from typing import Any, Protocol

import jwt
from jwt import PyJWTError

from app.security.principal import Principal

ASYMMETRIC_JWT_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)


class JwksClient(Protocol):
    def get_signing_key_from_jwt(self, token: str): ...


class JwtAuthenticationError(RuntimeError):
    code = "invalid_token"

    def __init__(self) -> None:
        super().__init__("身份令牌无效。")


class JwtConfigurationError(RuntimeError):
    pass


class JwtPrincipalProvider:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        allowed_algorithms: tuple[str, ...] | list[str],
        public_key: str | bytes | None = None,
        jwks_client: JwksClient | None = None,
    ) -> None:
        algorithms = tuple(dict.fromkeys(allowed_algorithms))
        if not issuer.strip() or not audience.strip():
            raise JwtConfigurationError("JWT issuer 和 audience 不能为空。")
        if not algorithms or not set(algorithms).issubset(ASYMMETRIC_JWT_ALGORITHMS):
            raise JwtConfigurationError("JWT 只允许配置受支持的非对称签名算法。")
        if (public_key is None) == (jwks_client is None):
            raise JwtConfigurationError("JWT 必须且只能配置 public_key 或 JWKS client。")

        self._issuer = issuer
        self._audience = audience
        self._allowed_algorithms = algorithms
        self._public_key = public_key
        self._jwks_client = jwks_client

    def authenticate(self, token: str) -> Principal:
        try:
            if not token.strip():
                raise ValueError("empty token")
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in self._allowed_algorithms:
                raise ValueError("algorithm is not allowed")
            key = self._resolve_key(token)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self._allowed_algorithms),
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["exp", "sub", "tenant_id"],
                    "strict_aud": True,
                },
            )
            expiry = claims["exp"]
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
                raise TypeError("invalid expiration")
            expiry = float(expiry)
            if not isfinite(expiry):
                raise ValueError("invalid expiration")
            return Principal(
                tenant_id=_required_text(claims, "tenant_id"),
                user_id=_required_text(claims, "sub"),
                scopes=_string_collection(claims.get("scope", claims.get("scopes"))),
                token_id=_optional_text(claims, "jti"),
                roles=_string_collection(claims.get("roles")),
                expires_at=expiry,
            )
        except (PyJWTError, TypeError, ValueError, KeyError, AttributeError, OverflowError) as exc:
            raise JwtAuthenticationError() from exc

    def _resolve_key(self, token: str) -> Any:
        if self._public_key is not None:
            return self._public_key
        if self._jwks_client is None:
            raise JwtConfigurationError("JWT key source 未配置。")
        return self._jwks_client.get_signing_key_from_jwt(token).key


def build_jwks_client(
    *,
    url: str,
    cache_ttl_seconds: int,
    timeout_seconds: int = 10,
) -> JwksClient:
    return jwt.PyJWKClient(
        url,
        cache_keys=True,
        max_cached_keys=16,
        cache_jwk_set=True,
        lifespan=cache_ttl_seconds,
        timeout=timeout_seconds,
    )


def _required_text(claims: dict[str, Any], name: str) -> str:
    value = claims[name]
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"invalid {name}")
    return value


def _optional_text(claims: dict[str, Any], name: str) -> str | None:
    value = claims.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {name}")
    return value.strip()


def _string_collection(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = value.split()
    elif isinstance(value, list):
        items = value
    else:
        raise TypeError("claim must be a string or string array")
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise ValueError("claim contains an invalid item")
    return tuple(dict.fromkeys(item.strip() for item in items))
