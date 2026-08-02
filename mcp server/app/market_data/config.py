from __future__ import annotations

import os
from json import JSONDecodeError
import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from app.market_data.provider import (
    MarketDataError,
    MarketDataProvider,
    UnavailableMarketDataProvider,
)
from app.market_data.tushare_provider import DEFAULT_BASE_URL, TushareHttpClient, TushareMarketDataProvider


ProviderStatus = Literal["configured", "not_configured", "configuration_unavailable"]
ProviderName = Literal["tushare"]
ADMIN_NO_ACTIVE_PROVIDER_DETAIL = "未配置激活的数据源。"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class MarketDataProviderSettings(BaseModel):
    """MCP 进程中解析后的市场数据源配置快照。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: ProviderStatus = "not_configured"
    provider: ProviderName | None = None
    token: SecretStr | None = Field(default=None, repr=False, exclude=True)
    token_env_var: str = "TUSHARE_TOKEN"
    enabled_api_names: set[str] = Field(default_factory=set)
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    stock_basic_cache_ttl_seconds: int = Field(default=3600, ge=0, le=86_400)
    error: MarketDataError | None = None

    @model_validator(mode="after")
    def validate_status_consistency(self) -> MarketDataProviderSettings:
        token = self.token.get_secret_value().strip() if self.token is not None else ""
        if self.status == "configured":
            if self.provider != "tushare":
                raise ValueError("configured 市场数据源必须是 tushare。")
            if not token:
                raise ValueError("configured 市场数据源必须包含 token。")
            if not self.enabled_api_names:
                raise ValueError("configured 市场数据源必须声明 enabled_api_names。")
            if self.error is not None:
                raise ValueError("configured 市场数据源不能携带 error。")
        elif self.status == "not_configured":
            if self.provider is not None or self.error is not None or self.token is not None:
                raise ValueError("not_configured 市场数据源不能携带 provider、token 或 error。")
        elif self.token is not None:
            raise ValueError("configuration_unavailable 市场数据源不能携带 token。")
        elif self.error is None:
            raise ValueError("configuration_unavailable 市场数据源必须携带 error。")
        return self


def load_market_data_provider_settings() -> MarketDataProviderSettings:
    admin_base_url = os.getenv("MCP_MARKET_DATA_ADMIN_BASE_URL", "").strip()
    if admin_base_url:
        return _settings_from_admin(admin_base_url)
    return _settings_from_explicit_local_env()


def build_market_data_provider(
    settings: MarketDataProviderSettings,
    *,
    tushare_client: TushareHttpClient | None = None,
) -> MarketDataProvider:
    token = _token_value(settings)
    if settings.status != "configured":
        return UnavailableMarketDataProvider(settings.error or _error_for_status(settings))
    if settings.provider != "tushare" or not token or not settings.enabled_api_names:
        return UnavailableMarketDataProvider(
            settings.error
            or _configuration_invalid_error(
                reason="configured_settings_inconsistent",
                token_env_var=settings.token_env_var,
            )
        )
    return TushareMarketDataProvider(
        token=token,
        client=tushare_client,
        enabled_api_names=settings.enabled_api_names,
        base_url=settings.base_url,
        timeout_seconds=settings.timeout_seconds,
        stock_basic_cache_ttl_seconds=settings.stock_basic_cache_ttl_seconds,
    )


def build_configured_market_data_provider() -> MarketDataProvider:
    return build_market_data_provider(load_market_data_provider_settings())


def _settings_from_admin(admin_base_url: str) -> MarketDataProviderSettings:
    try:
        timeout_seconds = float(os.getenv("MCP_MARKET_DATA_ADMIN_TIMEOUT_SECONDS", "2"))
    except ValueError:
        return _configuration_invalid_settings(reason="admin_timeout_invalid")
    try:
        response = httpx.get(
            f"{admin_base_url.rstrip('/')}/api/market-data-providers/active",
            timeout=timeout_seconds,
        )
    except httpx.RequestError:
        return _unavailable_settings(_configuration_unavailable_error(reason="admin_request_failed"))

    if response.status_code == 404:
        try:
            payload = response.json()
        except (JSONDecodeError, ValueError):
            return _unavailable_settings(
                _configuration_unavailable_error(
                    reason="admin_response_invalid",
                    response=response,
                )
            )
        if isinstance(payload, dict) and payload.get("detail") == ADMIN_NO_ACTIVE_PROVIDER_DETAIL:
            return MarketDataProviderSettings()
        return _unavailable_settings(
            _configuration_unavailable_error(reason="admin_http_status_error", response=response)
        )
    if response.status_code < 200 or response.status_code >= 300:
        return _unavailable_settings(
            _configuration_unavailable_error(reason="admin_http_status_error", response=response)
        )

    try:
        payload = response.json()
    except (JSONDecodeError, ValueError):
        return _unavailable_settings(
            _configuration_unavailable_error(reason="admin_response_invalid", response=response)
        )
    if not isinstance(payload, dict):
        return _unavailable_settings(
            _configuration_unavailable_error(reason="admin_response_invalid", response=response)
        )
    try:
        return _settings_from_admin_payload(payload)
    except (TypeError, ValueError, ValidationError):
        return _unavailable_settings(
            _configuration_unavailable_error(reason="admin_configuration_invalid", response=response)
        )


def _settings_from_explicit_local_env() -> MarketDataProviderSettings:
    if os.getenv("MCP_MARKET_DATA_PROVIDER", "").strip().lower() != "tushare":
        return MarketDataProviderSettings()

    token_env_var = os.getenv("MCP_TUSHARE_TOKEN_ENV_VAR", "TUSHARE_TOKEN").strip()
    if not token_env_var:
        return _configuration_invalid_settings(reason="token_env_var_missing")
    raw_enabled_api_names = os.getenv("MCP_TUSHARE_ENABLED_API_NAMES", "")
    try:
        enabled_api_names = _parse_enabled_api_names(raw_enabled_api_names)
        token = os.getenv(token_env_var, "").strip()
        if not token:
            return _configuration_invalid_settings(
                provider="tushare",
                token_env_var=token_env_var,
                enabled_api_names=enabled_api_names,
                reason="token_missing",
            )
        return MarketDataProviderSettings(
            status="configured",
            provider="tushare",
            token=token,
            token_env_var=token_env_var,
            enabled_api_names=enabled_api_names,
            base_url=os.getenv("MCP_TUSHARE_BASE_URL", DEFAULT_BASE_URL).strip(),
            timeout_seconds=float(os.getenv("MCP_TUSHARE_TIMEOUT_SECONDS", "10")),
            stock_basic_cache_ttl_seconds=int(
                os.getenv("MCP_TUSHARE_STOCK_BASIC_CACHE_TTL_SECONDS", "3600")
            ),
        )
    except (TypeError, ValidationError):
        return _configuration_invalid_settings(
            provider="tushare",
            token_env_var=token_env_var,
            reason="local_configuration_invalid",
        )
    except ValueError:
        return _configuration_invalid_settings(
            provider="tushare",
            token_env_var=token_env_var,
            reason=(
                "enabled_api_names_missing"
                if not raw_enabled_api_names.strip()
                else "local_configuration_invalid"
            ),
        )


def _settings_from_admin_payload(payload: dict[str, Any]) -> MarketDataProviderSettings:
    if payload.get("provider") != "tushare":
        raise ValueError("Admin 返回了不支持的市场数据源。")
    raw_settings = payload.get("settings")
    if not isinstance(raw_settings, dict):
        raise ValueError("Admin 返回的市场数据源 settings 非法。")
    token_env_var = raw_settings.get("token_env_var")
    enabled_api_names = raw_settings.get("enabled_api_names")
    if not isinstance(token_env_var, str) or not token_env_var.strip():
        raise ValueError("Admin 未配置 Tushare token_env_var。")
    if not isinstance(enabled_api_names, list):
        raise ValueError("Admin 未配置有效的 enabled_api_names。")
    normalized_api_names = _parse_enabled_api_names(enabled_api_names)
    token_env_var = token_env_var.strip()
    token = os.getenv(token_env_var, "").strip()
    if not token:
        return _configuration_invalid_settings(
            provider="tushare",
            token_env_var=token_env_var,
            enabled_api_names=normalized_api_names,
            reason="token_missing",
        )
    raw_cache_ttl = raw_settings.get("stock_basic_cache_ttl_seconds")
    cache_ttl = 3600 if raw_cache_ttl is None else int(raw_cache_ttl)
    return MarketDataProviderSettings(
        status="configured",
        provider="tushare",
        token=token,
        token_env_var=token_env_var,
        enabled_api_names=normalized_api_names,
        base_url=str(raw_settings.get("base_url") or DEFAULT_BASE_URL).strip(),
        timeout_seconds=float(raw_settings.get("timeout_seconds") or 10),
        stock_basic_cache_ttl_seconds=cache_ttl,
    )


def _parse_enabled_api_names(value: str | list[Any]) -> set[str]:
    raw_names = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw_names, list):
        raise ValueError("enabled_api_names 必须是列表或逗号分隔字符串。")
    if any(not isinstance(name, str) or not name.strip() for name in raw_names):
        raise ValueError("enabled_api_names 包含非法接口名。")
    names = {name.strip() for name in raw_names}
    if not names:
        raise ValueError("enabled_api_names 不能为空。")
    return names


def _unavailable_settings(error: MarketDataError) -> MarketDataProviderSettings:
    return MarketDataProviderSettings(
        status="configuration_unavailable",
        provider=None,
        error=error,
    )


def _configuration_invalid_settings(
    *,
    provider: ProviderName | None = None,
    token_env_var: str = "TUSHARE_TOKEN",
    enabled_api_names: set[str] | None = None,
    reason: str,
) -> MarketDataProviderSettings:
    return MarketDataProviderSettings(
        status="configuration_unavailable",
        provider=provider,
        token_env_var=token_env_var,
        enabled_api_names=enabled_api_names or set(),
        error=_configuration_invalid_error(reason=reason, token_env_var=token_env_var),
    )


def _token_value(settings: MarketDataProviderSettings) -> str:
    if not isinstance(settings.token, SecretStr):
        return ""
    return settings.token.get_secret_value().strip()


def _error_for_status(settings: MarketDataProviderSettings) -> MarketDataError:
    if settings.status == "not_configured":
        return MarketDataError(
            "market_data_source_not_configured",
            "未配置市场数据源，请在 AI 中台启用并配置 Tushare 数据源后重试。",
            retryable=False,
        )
    return _configuration_unavailable_error(reason="configuration_unavailable")


def _configuration_unavailable_error(
    *,
    reason: str,
    response: httpx.Response | Any | None = None,
) -> MarketDataError:
    details: dict[str, Any] = {"reason": reason}
    if response is not None:
        details["http_status"] = response.status_code
        request_id = _safe_request_id(response)
        if request_id is not None:
            details["request_id"] = request_id
    retryable = response is None or response.status_code in {408, 429} or response.status_code >= 500
    return MarketDataError(
        "market_data_configuration_unavailable",
        "市场数据源配置暂不可用，请稍后重试或联系管理员检查 AI 中台配置。",
        details=details,
        retryable=retryable,
    )


def _configuration_invalid_error(*, reason: str, token_env_var: str | None = None) -> MarketDataError:
    details: dict[str, Any] = {"reason": reason}
    if token_env_var:
        details["token_env_var"] = token_env_var
    return MarketDataError(
        "market_data_configuration_invalid",
        "市场数据源配置不完整，请联系管理员检查 Tushare 配置。",
        details=details,
        retryable=False,
    )


def _safe_request_id(response: httpx.Response | Any) -> str | None:
    headers = getattr(response, "headers", {})
    request_id = headers.get("x-request-id") if hasattr(headers, "get") else None
    if isinstance(request_id, str) and _SAFE_REQUEST_ID.fullmatch(request_id):
        return request_id
    return None
