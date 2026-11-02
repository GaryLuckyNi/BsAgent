import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

RiskLevel = Literal["low", "medium", "high"]
MarketDataProviderName = Literal["tushare"]


class BusinessIntentCreate(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    intent_type: str = Field(min_length=1, max_length=64)
    domain: str = Field(min_length=1, max_length=128)
    subdomain: str = Field(min_length=1, max_length=128)
    trigger_keywords: list[str] = Field(default_factory=list)
    trigger_examples: list[str] = Field(default_factory=list)
    enabled: bool = True
    description: str | None = None


class BusinessIntentUpdate(BaseModel):
    intent_type: str | None = Field(default=None, min_length=1, max_length=64)
    domain: str | None = Field(default=None, min_length=1, max_length=128)
    subdomain: str | None = Field(default=None, min_length=1, max_length=128)
    trigger_keywords: list[str] | None = None
    trigger_examples: list[str] | None = None
    enabled: bool | None = None
    description: str | None = None


class BusinessIntentRead(BusinessIntentCreate):
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SkillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    version: str = Field(default="1.0.0", min_length=1, max_length=64)
    summary: str = Field(min_length=1)
    supported_intents: list[str] = Field(default_factory=list)
    supported_domains: list[str] = Field(default_factory=list)
    supported_sub_intents: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = "medium"
    enabled: bool = True

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class SkillUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    version: str | None = Field(default=None, min_length=1, max_length=64)
    summary: str | None = Field(default=None, min_length=1)
    supported_intents: list[str] | None = None
    supported_domains: list[str] | None = None
    supported_sub_intents: list[str] | None = None
    risk_level: RiskLevel | None = None
    enabled: bool | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class SkillRead(SkillCreate):
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MarketDataProviderConfigCreate(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    provider: MarketDataProviderName
    display_name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    active: bool = False
    settings: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None

    @field_validator("settings", mode="before")
    @classmethod
    def validate_settings_object(cls, value: Any) -> dict[str, Any]:
        return validate_market_data_provider_settings_object(value)

    @field_validator("provider", mode="before")
    @classmethod
    def validate_provider(cls, value: Any) -> Any:
        return validate_market_data_provider_name(value)

    @model_validator(mode="after")
    def validate_provider_settings(self):
        self.settings = validate_market_data_provider_settings(
            provider=self.provider,
            settings=self.settings,
        )
        if self.active and not self.enabled:
            raise ValueError("不能将停用的数据源配置设置为 active。")
        return self


class MarketDataProviderConfigUpdate(BaseModel):
    provider: MarketDataProviderName | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    active: bool | None = None
    settings: dict[str, Any] | None = None
    description: str | None = None

    @field_validator("settings", mode="before")
    @classmethod
    def validate_settings_object(cls, value: Any) -> dict[str, Any]:
        return validate_market_data_provider_settings_object(value)

    @field_validator("provider", mode="before")
    @classmethod
    def validate_provider(cls, value: Any) -> Any:
        if value is None:
            return value
        return validate_market_data_provider_name(value)


class MarketDataProviderConfigRead(BaseModel):
    id: str
    provider: MarketDataProviderName
    display_name: str
    enabled: bool
    active: bool
    settings: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MarketDataMcpConnectionRead(BaseModel):
    provider_id: str
    provider: MarketDataProviderName
    server_name: str
    server_url: str
    config_revision: str
    public_documentation: bool = False
    documentation_domains: list[str] = Field(default_factory=list)


class Page(BaseModel):
    items: list
    total: int


def validate_market_data_provider_settings_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PydanticCustomError(
            "market_data_settings_type",
            "数据源 settings 必须是 JSON object。",
        )
    return value


def validate_market_data_provider_settings(
    *,
    provider: MarketDataProviderName,
    settings: dict[str, Any],
) -> dict[str, Any]:
    if provider != "tushare":
        raise ValueError("数据源类型只允许为 tushare。")
    try:
        return TushareSettings.model_validate(settings).model_dump()
    except ValidationError as exc:
        first_error = exc.errors()[0]
        validation_error = first_error.get("ctx", {}).get("error")
        if isinstance(validation_error, ValueError):
            raise validation_error from exc
        raise ValueError(first_error["msg"]) from exc


class TushareSettings(BaseModel):
    mcp_server_url: str
    public_documentation: bool = True
    documentation_domains: list[str] = Field(default_factory=lambda: ["tushare.pro"], max_length=5)

    model_config = {"extra": "allow"}

    @field_validator("documentation_domains")
    @classmethod
    def validate_documentation_domains(cls, domains):
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value) or "." not in value
               or ".." in value for value in domains):
            raise ValueError("文档域名必须为站点域名，不能包含协议、路径或凭证。")
        return sorted(set(domains))

    @model_validator(mode="before")
    @classmethod
    def validate_required_settings(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "mcp_server_url" not in value:
            raise ValueError("Tushare 数据源必须配置 settings.mcp_server_url。")
        return value

    @field_validator("mcp_server_url", mode="before")
    @classmethod
    def validate_mcp_server_url(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Tushare 数据源必须配置 settings.mcp_server_url。")
        normalized = value.strip()
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("settings.mcp_server_url 必须是有效的 HTTP(S) URL。")
        if "******" in normalized:
            raise ValueError("settings.mcp_server_url 不能使用脱敏占位值。")
        return normalized


def validate_market_data_provider_name(value: Any) -> Any:
    if value != "tushare":
        raise ValueError("数据源类型只允许为 tushare。")
    return value
