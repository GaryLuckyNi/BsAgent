from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)


CoverageStatus = Literal["complete", "partial", "failed"]
MARKET_DATA_METADATA_FIELDS = frozenset(
    {
        "status",
        "coverage",
        "source_provider",
        "as_of_date",
        "warnings",
        "query",
        "symbol",
        "period",
        "date_range",
    }
)


class FieldSelection(BaseModel):
    """业务工具的字段需求。

    Provider 允许仅声明 optional 字段，以支持尽力而为的增强查询；
    Planner/Skill 仍应将用户明确询问的字段声明为 required。
    """

    model_config = ConfigDict(extra="forbid")

    required_fields: list[str] = Field(default_factory=list)
    optional_fields: list[str] = Field(default_factory=list)

    @field_validator("required_fields", "optional_fields")
    @classmethod
    def normalize_fields(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            field_name = value.strip()
            if not field_name:
                raise ValueError("字段名不能为空。")
            if field_name not in seen:
                seen.add(field_name)
                normalized.append(field_name)
        return normalized

    @model_validator(mode="after")
    def validate_selection(self) -> FieldSelection:
        if not self.required_fields and not self.optional_fields:
            raise ValueError("至少声明一个必需或可选字段。")
        if set(self.required_fields) & set(self.optional_fields):
            raise ValueError("必需字段与可选字段不能重复。")
        return self

    @property
    def requested_fields(self) -> list[str]:
        return [*self.required_fields, *self.optional_fields]


class ToolCoverage(BaseModel):
    """工具返回的字段覆盖率，不包含任何凭证或上游原始响应。"""

    status: CoverageStatus = "complete"
    requested_fields: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    optional_fields: list[str] = Field(default_factory=list)
    provided_fields: list[str] = Field(default_factory=list)
    missing_required_fields: list[str] = Field(default_factory=list)
    missing_optional_fields: list[str] = Field(default_factory=list)
    source_apis: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def derive_status(self) -> ToolCoverage:
        if self.missing_required_fields:
            self.status = "failed"
        elif self.missing_optional_fields:
            self.status = "partial"
        else:
            self.status = "complete"
        return self


class MarketDataResult(BaseModel):
    status: CoverageStatus = "complete"
    coverage: ToolCoverage = Field(default_factory=ToolCoverage)

    @model_validator(mode="after")
    def align_status_with_coverage(self) -> MarketDataResult:
        self.status = self.coverage.status
        return self

    @model_serializer(mode="plain")
    def serialize_requested_fields(self) -> dict[str, Any]:
        """从运行时子类取字段，避免嵌套在基类注解下丢失业务数据。"""

        provided_fields = set(self.coverage.provided_fields)
        included_fields = MARKET_DATA_METADATA_FIELDS | provided_fields
        runtime_fields = type(self).model_fields
        if "metrics" in runtime_fields and provided_fields:
            included_fields = included_fields | {"metrics"}
        if "announcements" in runtime_fields and "announcements" in provided_fields:
            included_fields = included_fields | {"announcements"}
        return {
            field_name: getattr(self, field_name)
            for field_name in runtime_fields
            if field_name in included_fields
        }


class CompanyResolution(MarketDataResult):
    query: str
    symbol: str
    name: str | None = None
    exchange: str | None = None
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class CompanyProfile(MarketDataResult):
    symbol: str
    name: str | None = None
    industry: str | None = None
    board: str | None = None
    main_business: str | None = None
    registered_address: str | None = None
    controller: str | None = None
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class MarketSnapshot(MarketDataResult):
    symbol: str
    price: float | None = None
    market_cap: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    pct_change: float | None = None
    turnover: float | None = None
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class PriceTrend(MarketDataResult):
    symbol: str
    window_days: int
    latest_price: float | None = None
    return_20d: float | None = None
    return_60d: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    volatility_20d: float | None = None
    max_drawdown: float | None = None
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class FinancialMetrics(MarketDataResult):
    symbol: str
    period: str
    metrics: dict[str, float | int | None]
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class AnnouncementSearchResult(MarketDataResult):
    symbol: str
    date_range: str
    announcements: list[dict[str, Any]]
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)
