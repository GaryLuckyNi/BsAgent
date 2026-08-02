import importlib.util
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.security.jwt_provider import (
    JwtAuthenticationError,
    JwtConfigurationError,
    JwtPrincipalProvider,
)


def test_jwt_provider_module_is_available():
    assert importlib.util.find_spec("app.security.jwt_provider") is not None
    assert importlib.util.find_spec("app.security.principal") is not None


@pytest.fixture(scope="module")
def rsa_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )


def _claims(**overrides):
    claims = {
        "sub": "user-1",
        "tenant_id": "tenant-1",
        "iss": "https://identity.example.test",
        "aud": "bs-agent",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "scope": "agent:invoke rag:retrieve",
        "roles": ["analyst"],
        "jti": "token-1",
    }
    claims.update(overrides)
    return claims


def _provider(public_key):
    return JwtPrincipalProvider(
        issuer="https://identity.example.test",
        audience="bs-agent",
        allowed_algorithms=("RS256",),
        public_key=public_key,
    )


def _token(private_key, *, claims=None, algorithm="RS256"):
    return jwt.encode(claims or _claims(), private_key, algorithm=algorithm)


def test_jwt_provider_returns_immutable_trusted_principal(rsa_keys):
    private_key, public_key = rsa_keys

    principal = _provider(public_key).authenticate(_token(private_key))

    assert principal.user_id == "user-1"
    assert principal.tenant_id == "tenant-1"
    assert principal.scopes == ("agent:invoke", "rag:retrieve")
    assert principal.roles == ("analyst",)
    assert principal.token_id == "token-1"
    with pytest.raises(FrozenInstanceError):
        principal.user_id = "attacker"


@pytest.mark.parametrize(
    "claims",
    [
        _claims(iss="https://wrong-issuer.example.test"),
        _claims(aud="another-service"),
        _claims(exp=datetime.now(UTC) - timedelta(seconds=1)),
        {key: value for key, value in _claims().items() if key != "sub"},
        {key: value for key, value in _claims().items() if key != "tenant_id"},
    ],
)
def test_jwt_provider_rejects_invalid_registered_and_identity_claims(rsa_keys, claims):
    private_key, public_key = rsa_keys

    with pytest.raises(JwtAuthenticationError) as exc_info:
        _provider(public_key).authenticate(_token(private_key, claims=claims))

    assert str(exc_info.value) == "身份令牌无效。"


def test_jwt_provider_rejects_wrong_signature(rsa_keys):
    _, public_key = rsa_keys
    attacker_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with pytest.raises(JwtAuthenticationError):
        _provider(public_key).authenticate(_token(attacker_private))


@pytest.mark.parametrize("scope", [{"admin": True}, ["agent:invoke", 7], 7])
def test_jwt_provider_rejects_malformed_scopes(rsa_keys, scope):
    private_key, public_key = rsa_keys

    with pytest.raises(JwtAuthenticationError):
        _provider(public_key).authenticate(
            _token(private_key, claims=_claims(scope=scope))
        )


def test_jwt_provider_rejects_algorithm_outside_allowlist(rsa_keys):
    _, public_key = rsa_keys
    token = jwt.encode(_claims(), "shared-secret", algorithm="HS256")

    with pytest.raises(JwtAuthenticationError):
        _provider(public_key).authenticate(token)


def test_jwt_provider_requires_exactly_one_key_source():
    common = {
        "issuer": "https://identity.example.test",
        "audience": "bs-agent",
        "allowed_algorithms": ("RS256",),
    }

    with pytest.raises(JwtConfigurationError):
        JwtPrincipalProvider(**common)
    with pytest.raises(JwtConfigurationError):
        JwtPrincipalProvider(
            **common,
            public_key="public-key",
            jwks_client=object(),
        )
