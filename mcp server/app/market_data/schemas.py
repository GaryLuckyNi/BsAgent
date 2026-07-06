from typing import Any

from pydantic import BaseModel, Field


class CompanyResolution(BaseModel):
    query: str
    symbol: str
    name: str
    exchange: str
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class CompanyProfile(BaseModel):
    symbol: str
    name: str
    industry: str
    board: str
    main_business: str
    registered_address: str | None = None
    controller: str | None = None
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class MarketSnapshot(BaseModel):
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


class FinancialMetrics(BaseModel):
    symbol: str
    period: str
    metrics: dict[str, float | int | None]
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class AnnouncementSearchResult(BaseModel):
    symbol: str
    date_range: str
    announcements: list[dict[str, Any]]
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class RiskEvents(BaseModel):
    symbol: str
    date_range: str
    events: list[dict[str, Any]]
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)
