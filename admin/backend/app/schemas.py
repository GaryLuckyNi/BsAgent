from datetime import datetime
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError


IntentKind = Literal[
    "knowledge_qa",
    "memory_query",
    "tool_action",
    "task_workflow",
    "conversation",
    "clarification_needed",
    "unsupported",
]
RiskLevel = Literal["low", "medium", "high"]
MarketDataProviderName = Literal["tushare"]
_MCP_MARKET_TOOL_PATTERN = re.compile(
    r"^mcp\..+\.(?P<capability>stock\.[a-z0-9_]+)$"
)


class ToolEvidenceRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(pattern=r"^stock\.[a-z0-9_]+$")
    required_fields: list[str] = Field(default_factory=list)
    optional_fields: list[str] = Field(default_factory=list)

    @field_validator("capability", mode="before")
    @classmethod
    def normalize_capability(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("证据能力不能为空。")
        return value.strip()

    @field_validator("required_fields", "optional_fields", mode="before")
    @classmethod
    def normalize_fields(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("证据字段必须是列表。")
        normalized: list[str] = []
        seen: set[str] = set()
        for field_name in value:
            if not isinstance(field_name, str) or not field_name.strip():
                raise ValueError("证据字段不能为空。")
            normalized_name = field_name.strip()
            if normalized_name not in seen:
                normalized.append(normalized_name)
                seen.add(normalized_name)
        return normalized

    @model_validator(mode="after")
    def validate_field_overlap(self):
        if set(self.required_fields) & set(self.optional_fields):
            raise ValueError("必需字段与可选字段不能重复。")
        if not self.required_fields and not self.optional_fields:
            raise ValueError("市场数据证据要求至少需要一个字段。")
        return self


class SkillEvidenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: list[ToolEvidenceRequirement] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_duplicate_capabilities(self):
        capabilities = [requirement.capability for requirement in self.requirements]
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("同一证据契约中 capability 不能重复。")
        return self


class BusinessIntentCreate(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    intent: IntentKind
    domain: str = Field(min_length=1, max_length=128)
    sub_intent: str = Field(min_length=1, max_length=128)
    candidate_skill: str | None = Field(default=None, max_length=128)
    risk_level: RiskLevel = "medium"
    trigger_keywords: list[str] = Field(default_factory=list)
    trigger_examples: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    enabled: bool = True
    description: str | None = None


class BusinessIntentUpdate(BaseModel):
    intent: IntentKind | None = None
    domain: str | None = Field(default=None, min_length=1, max_length=128)
    sub_intent: str | None = Field(default=None, min_length=1, max_length=128)
    candidate_skill: str | None = Field(default=None, max_length=128)
    risk_level: RiskLevel | None = None
    trigger_keywords: list[str] | None = None
    trigger_examples: list[str] | None = None
    required_capabilities: list[str] | None = None
    enabled: bool | None = None
    description: str | None = None


class BusinessIntentRead(BusinessIntentCreate):
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(default="1.0.0", min_length=1, max_length=64)
    summary: str = Field(min_length=1)
    supported_intents: list[str] = Field(default_factory=list)
    supported_domains: list[str] = Field(default_factory=list)
    supported_sub_intents: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    evidence_contract: SkillEvidenceContract = Field(default_factory=SkillEvidenceContract)
    risk_level: RiskLevel = "medium"
    enabled: bool = True

    @model_validator(mode="after")
    def validate_market_tool_evidence_contract(self):
        validate_skill_evidence_contract_alignment(
            allowed_tools=self.allowed_tools,
            evidence_contract=self.evidence_contract,
        )
        return self


class SkillUpdate(BaseModel):
    version: str | None = Field(default=None, min_length=1, max_length=64)
    summary: str | None = Field(default=None, min_length=1)
    supported_intents: list[str] | None = None
    supported_domains: list[str] | None = None
    supported_sub_intents: list[str] | None = None
    allowed_tools: list[str] | None = None
    evidence_contract: SkillEvidenceContract = Field(default=None)
    risk_level: RiskLevel | None = None
    enabled: bool | None = None


class SkillRead(SkillCreate):
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


def validate_skill_evidence_contract_alignment(
    *,
    allowed_tools: list[str],
    evidence_contract: SkillEvidenceContract,
) -> None:
    market_capabilities = [
        match.group("capability")
        for tool_name in allowed_tools
        if (match := _MCP_MARKET_TOOL_PATTERN.fullmatch(tool_name)) is not None
    ]
    if len(market_capabilities) != len(set(market_capabilities)):
        raise ValueError("allowed_tools 中市场数据 capability 不能重复。")

    requirement_capabilities = [
        requirement.capability for requirement in evidence_contract.requirements
    ]
    missing_requirements = sorted(set(market_capabilities) - set(requirement_capabilities))
    missing_tools = sorted(set(requirement_capabilities) - set(market_capabilities))
    if missing_requirements or missing_tools:
        details = []
        if missing_requirements:
            details.append(f"缺少证据要求：{', '.join(missing_requirements)}")
        if missing_tools:
            details.append(f"缺少允许工具：{', '.join(missing_tools)}")
        raise ValueError("；".join(details))


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
    token_env_var: str
    enabled_api_names: list[str]

    model_config = {"extra": "allow"}

    @model_validator(mode="before")
    @classmethod
    def validate_required_settings(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "token_env_var" not in value:
            raise ValueError("Tushare 数据源必须配置 settings.token_env_var。")
        if "enabled_api_names" not in value:
            raise ValueError("Tushare 数据源必须配置非空 settings.enabled_api_names。")
        return value

    @field_validator("token_env_var", mode="before")
    @classmethod
    def validate_token_env_var(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Tushare 数据源必须配置 settings.token_env_var。")
        return value.strip()

    @field_validator("enabled_api_names", mode="before")
    @classmethod
    def normalize_enabled_api_names(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("Tushare 数据源的 settings.enabled_api_names 必须是列表。")
        normalized: list[str] = []
        seen: set[str] = set()
        for api_name in value:
            if not isinstance(api_name, str):
                raise ValueError("Tushare 数据源的 settings.enabled_api_names 每项必须是字符串。")
            normalized_name = api_name.strip()
            if normalized_name and normalized_name not in seen:
                normalized.append(normalized_name)
                seen.add(normalized_name)
        if not normalized:
            raise ValueError("Tushare 数据源必须配置非空 settings.enabled_api_names。")
        return normalized


def validate_market_data_provider_name(value: Any) -> Any:
    if value != "tushare":
        raise ValueError("数据源类型只允许为 tushare。")
    return value
