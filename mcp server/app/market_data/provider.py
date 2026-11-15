from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from app.market_data.schemas import (
    AnnouncementSearchResult,
    CompanyProfile,
    CompanyResolution,
    FinancialMetrics,
    MarketSnapshot,
    PriceTrend,
)


class MarketDataError(RuntimeError):
    """市场数据能力的结构化错误，供 MCP 层转换为标准错误 envelope。"""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = retryable


class MarketDataProvider(Protocol):
    def resolve_company(self, query: str) -> CompanyResolution: ...

    def get_company_profile(self, symbol: str) -> CompanyProfile: ...

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot: ...

    def get_price_trend(self, symbol: str, window_days: int) -> PriceTrend: ...

    def get_financial_metrics(self, symbol: str, period: str) -> FinancialMetrics: ...

    def search_announcements(self, symbol: str, date_range: str) -> AnnouncementSearchResult: ...


@dataclass(frozen=True)
class _MarketDataErrorDescriptor:
    code: str
    message: str
    details: Mapping[str, Any]
    retryable: bool

    @classmethod
    def from_error(cls, error: MarketDataError) -> _MarketDataErrorDescriptor:
        return cls(
            code=error.code,
            message=error.message,
            details=MappingProxyType(deepcopy(error.details)),
            retryable=error.retryable,
        )

    def to_error(self) -> MarketDataError:
        return MarketDataError(
            code=self.code,
            message=self.message,
            details=deepcopy(dict(self.details)),
            retryable=self.retryable,
        )


class UnavailableMarketDataProvider:
    """保留 MCP 服务启动能力，并让业务调用返回独立的结构化错误。"""

    def __init__(self, error: MarketDataError) -> None:
        self._error_descriptor = _MarketDataErrorDescriptor.from_error(error)

    def resolve_company(self, query: str) -> CompanyResolution:
        raise self._new_error()

    def get_company_profile(self, symbol: str) -> CompanyProfile:
        raise self._new_error()

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        raise self._new_error()

    def get_price_trend(self, symbol: str, window_days: int) -> PriceTrend:
        raise self._new_error()

    def get_financial_metrics(self, symbol: str, period: str) -> FinancialMetrics:
        raise self._new_error()

    def search_announcements(self, symbol: str, date_range: str) -> AnnouncementSearchResult:
        raise self._new_error()

    def _new_error(self) -> MarketDataError:
        return self._error_descriptor.to_error()
