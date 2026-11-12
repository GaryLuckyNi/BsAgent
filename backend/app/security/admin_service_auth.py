"""Admin client_credentials 只用于受信配置服务，不传播用户 token 或模型身份。"""

import math
from threading import Lock
from time import monotonic
from urllib.parse import urlparse

import httpx


def _origin(parsed):
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port or {"https": 443, "http": 80}.get(parsed.scheme),
    )


class AdminServiceAuthenticationError(RuntimeError):
    def __init__(self):
        super().__init__("admin_service_authentication_unavailable")


class AdminServiceAuth(httpx.Auth):
    def __init__(
        self,
        *,
        token_url,
        client_id,
        client_secret,
        scope,
        allowed_base_urls,
        audience="",
        token_client=None,
        allow_local_http=False,
    ):
        parsed = urlparse(token_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.query
            or not client_id
            or not client_secret
        ):
            raise ValueError("Admin 服务认证必须配置受信 HTTPS token endpoint 和服务客户端。")
        origins = set()
        for url in allowed_base_urls:
            parsed = urlparse(url)
            local_http = (
                allow_local_http
                and parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            )
            if (
                (parsed.scheme != "https" and not local_http)
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("Admin 服务目的地址必须为受信 HTTPS origin。")
            origins.add(_origin(parsed))
        self._origins = origins
        self._token_url = token_url
        self._client_id, self._client_secret = client_id, client_secret
        self._scope, self._audience = scope, audience
        self._token_client = token_client
        self._token, self._expires_at = "", 0.0
        self._lock = Lock()

    def _access_token(self):
        with self._lock:
            if self._token and monotonic() < self._expires_at:
                return self._token
            data = {"grant_type": "client_credentials"}
            if self._scope:
                data["scope"] = self._scope
            if self._audience:
                data["audience"] = self._audience
            client = self._token_client or httpx.Client(timeout=5, follow_redirects=False)
            try:
                response = client.post(
                    self._token_url,
                    data=data,
                    follow_redirects=False,
                    auth=httpx.BasicAuth(self._client_id, self._client_secret),
                )
                if response.status_code != 200:
                    raise ValueError("invalid token response")
                payload = response.json()
                token, ttl = payload.get("access_token"), payload.get("expires_in")
                if (
                    payload.get("token_type", "").lower() != "bearer"
                    or not isinstance(token, str)
                    or not token
                    or len(token) > 32768
                    or any(char.isspace() for char in token)
                    or isinstance(ttl, bool)
                    or not isinstance(ttl, (int, float))
                    or not math.isfinite(ttl)
                    or ttl <= 0
                ):
                    raise ValueError("invalid token response")
                self._token = token
                self._expires_at = monotonic() + max(0, min(ttl, 3600) - min(10, ttl / 2))
                return token
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                raise AdminServiceAuthenticationError from None
            finally:
                if self._token_client is None:
                    client.close()

    def auth_flow(self, request):
        parsed = urlparse(str(request.url))
        allowed_paths = {
            "/api/runtime/business-intents",
            "/api/runtime/skills",
            "/api/internal/market-data-providers/active/mcp-connection",
        }
        if (
            _origin(parsed) not in self._origins
            or request.method != "GET"
            or (
                parsed.path not in allowed_paths
                and not parsed.path.startswith("/api/runtime/skills/")
            )
        ):
            raise AdminServiceAuthenticationError
        request.headers["Authorization"] = f"Bearer {self._access_token()}"
        yield request


def build_admin_service_auth(config):
    if config.admin_auth_mode == "mock":
        if config.app_env == "production":
            raise ValueError("生产 Admin 配置读取必须使用服务身份。")
        return None
    return AdminServiceAuth(
        token_url=config.admin_service_token_url,
        client_id=config.admin_service_client_id,
        client_secret=config.admin_service_client_secret.get_secret_value(),
        scope=config.admin_service_scope,
        audience=config.admin_service_audience,
        allowed_base_urls=[config.admin_base_url, config.skill_registry_base_url],
        allow_local_http=config.app_env != "production",
    )
