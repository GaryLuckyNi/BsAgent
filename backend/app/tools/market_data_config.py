from typing import Any
from urllib.parse import urlparse

import httpx

from app.tools.mcp_gateway import McpServerConfig


_ACTIVE_MCP_CONNECTION_PATH = (
    "/api/internal/market-data-providers/active/mcp-connection"
)


class AdminMarketDataConfigClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = client

    def get_active_mcp_config(self) -> McpServerConfig | None:
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        try:
            try:
                response = client.get(f"{self._base_url}{_ACTIVE_MCP_CONNECTION_PATH}")
            except httpx.HTTPError as exc:
                raise RuntimeError("market_data_config_unavailable") from exc
        finally:
            if self._client is None:
                client.close()

        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError(f"market_data_config_http_{response.status_code}")

        try:
            payload: dict[str, Any] = response.json()
            if payload.get("provider") != "tushare":
                raise ValueError("unsupported provider")
            server_name = _required_string(payload, "server_name")
            server_url = _required_http_url(payload, "server_url")
            config_revision = _required_string(payload, "config_revision")
            return McpServerConfig(
                name=server_name,
                url=server_url,
                timeout_seconds=self._timeout_seconds,
                enforce_local_rbac=False,
                deployment_revision="tushare-official",
                config_revision=config_revision,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("market_data_config_invalid_response") from exc


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {field}")
    return value.strip()


def _required_http_url(payload: dict[str, Any], field: str) -> str:
    value = _required_string(payload, field)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid {field}")
    return value
