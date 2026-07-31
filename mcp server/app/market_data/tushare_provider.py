from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from difflib import SequenceMatcher
from functools import wraps
import re
from math import sqrt
from statistics import pstdev
from threading import Lock
import time
from typing import Any

import httpx

from app.market_data.capabilities import (
    ApiRequirements,
    build_tool_coverage,
    default_field_selection,
    optional_permission_warnings,
    resolve_api_requirements,
)
from app.market_data.schemas import (
    AnnouncementSearchResult,
    CompanyProfile,
    CompanyResolution,
    FinancialMetrics,
    FieldSelection,
    MarketSnapshot,
    PriceTrend,
    ToolCoverage,
)
from app.market_data.provider import MarketDataError


SOURCE_PROVIDER = "tushare"
DEFAULT_BASE_URL = "https://api.tushare.pro"
STOCK_BASIC_FIELDS = [
    "ts_code",
    "symbol",
    "name",
    "fullname",
    "cnspell",
    "area",
    "industry",
    "market",
    "exchange",
]

# stock_basic 是公司身份缓存，固定保留匹配与交易所识别所需的技术字段。
# 其他 API 则严格根据本次业务字段生成 Tushare fields。
RAW_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "stock.company_profile": {
        "main_business": ("main_business", "introduction"),
        "registered_address": ("province", "city"),
    },
    "stock.market_snapshot": {
        "price": ("close",),
        "pct_change": ("pct_chg",),
        "turnover": ("amount",),
        "market_cap": ("total_mv",),
        "pe_ttm": ("pe_ttm",),
        "pb": ("pb",),
    },
    "stock.price_trend": {
        "latest_price": ("close",),
        "return_20d": ("close",),
        "return_60d": ("close",),
        "ma20": ("close",),
        "ma60": ("close",),
        "volatility_20d": ("close",),
        "max_drawdown": ("close",),
    },
    "stock.financial_metrics": {
        "revenue": ("revenue",),
        "net_profit_parent": ("n_income_attr_p",),
        "operating_cash_flow": ("n_cashflow_act",),
        "revenue_yoy": ("or_yoy",),
        "net_profit_yoy": ("netprofit_yoy",),
        "operating_cash_flow_yoy": ("ocf_yoy",),
        "gross_margin": ("grossprofit_margin",),
        "roe": ("roe",),
        "debt_to_asset": ("debt_to_assets",),
    },
    "stock.announcement_search": {
        "announcements": ("ann_date", "title", "url"),
    },
}

PROVIDER_ERROR_MESSAGES = {
    "provider_permission_denied": "市场数据服务拒绝访问该接口。",
    "provider_rate_limited": "市场数据服务请求频率受限。",
    "provider_authentication_failed": "市场数据服务认证失败。",
    "provider_invalid_request": "市场数据服务拒绝了无效请求。",
    "provider_upstream_error": "市场数据服务返回业务错误。",
    "provider_timeout": "市场数据服务请求超时。",
    "provider_unavailable": "市场数据服务当前不可用。",
    "provider_response_invalid": "市场数据服务返回了无效响应。",
}


class _UpstreamFieldMappingError(Exception):
    """上游字段无法安全映射；异常本身永不携带原始值。"""


def _sanitize_mapping_errors(capability: str, *, selection_index: int):
    """阻止上游原值通过转换异常进入 Agent 状态或工具错误。"""

    def decorator(method):
        @wraps(method)
        def wrapped(provider, *args, **kwargs):
            try:
                return method(provider, *args, **kwargs)
            except MarketDataError:
                raise
            except _UpstreamFieldMappingError:
                selection = kwargs.get("selection")
                if selection is None and len(args) > selection_index:
                    selection = args[selection_index]
                requirements = provider._requirements(capability, selection)
                raise MarketDataError(
                    code="provider_response_invalid",
                    message=PROVIDER_ERROR_MESSAGES["provider_response_invalid"],
                    details={
                        "capability": capability,
                        "source_apis": requirements.source_apis,
                    },
                    retryable=False,
                ) from None

        return wrapped

    return decorator


class TushareHttpClient:
    def __init__(
        self,
        *,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not token.strip():
            raise ValueError("Tushare token 不能为空。")
        self._token = token
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def query(
        self,
        api_name: str,
        *,
        params: dict[str, Any],
        fields: list[str],
    ) -> list[dict[str, Any]]:
        payload = {
            "api_name": api_name,
            "token": self._token,
            "params": params,
            "fields": ",".join(fields),
        }
        try:
            response = httpx.post(
                self._base_url,
                json=payload,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException:
            raise _provider_error(
                "provider_timeout", api_name=api_name, retryable=True
            ) from None
        except httpx.RequestError:
            raise _provider_error(
                "provider_unavailable", api_name=api_name, retryable=True
            ) from None

        if not 200 <= response.status_code < 300:
            raise _http_status_error(api_name, response.status_code) from None

        try:
            body = response.json()
        except ValueError:
            raise _provider_error(
                "provider_response_invalid", api_name=api_name
            ) from None
        if not isinstance(body, dict) or "code" not in body:
            raise _provider_error("provider_response_invalid", api_name=api_name) from None

        upstream_code = body["code"]
        if isinstance(upstream_code, bool) or not isinstance(upstream_code, (int, str)):
            raise _provider_error("provider_response_invalid", api_name=api_name) from None
        if upstream_code not in {0, "0"}:
            raise _business_error(
                api_name,
                upstream_code=upstream_code,
                upstream_message=body.get("msg"),
            ) from None

        data = body.get("data")
        if not isinstance(data, dict):
            raise _provider_error("provider_response_invalid", api_name=api_name) from None
        response_fields = data.get("fields")
        items = data.get("items")
        if (
            not isinstance(response_fields, list)
            or not all(isinstance(field, str) for field in response_fields)
            or not isinstance(items, list)
            or not all(isinstance(item, (list, tuple)) for item in items)
            or any(len(item) != len(response_fields) for item in items)
        ):
            raise _provider_error("provider_response_invalid", api_name=api_name) from None
        return [dict(zip(response_fields, item, strict=True)) for item in items]


class TushareMarketDataProvider:
    def __init__(
        self,
        *,
        token: str,
        enabled_api_names: set[str],
        client: TushareHttpClient | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 10.0,
        stock_basic_cache_ttl_seconds: int = 3600,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._client = client or TushareHttpClient(
            token=token,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
        self._enabled_api_names = set(enabled_api_names)
        self._stock_basic_cache_ttl_seconds = stock_basic_cache_ttl_seconds
        self._stock_basic_cached_at = 0.0
        self._stock_basic_cache: list[dict[str, Any]] | None = None
        self._stock_basic_cache_lock = Lock()
        self._today = today

    @_sanitize_mapping_errors("stock.resolve_company", selection_index=1)
    def resolve_company(
        self,
        query: str,
        selection: FieldSelection | None = None,
    ) -> CompanyResolution:
        requirements = self._requirements("stock.resolve_company", selection)
        stock = self._find_stock(query) if "stock_basic" in requirements.api_fields else None
        if stock is None:
            coverage = build_tool_coverage(requirements, set())
            candidates = self._rank_company_candidates(query)
            _raise_for_missing_required(
                "stock.resolve_company",
                coverage,
                extra_details={"candidates": candidates} if candidates else None,
            )
            return CompanyResolution(
                query=query,
                symbol="",
                name=None,
                exchange=None,
                source_provider=SOURCE_PROVIDER,
                as_of_date=self._as_of_date(),
                warnings=[
                    *optional_permission_warnings(requirements),
                    *(["tushare_company_not_found"] if requirements.api_fields else []),
                ],
                status=coverage.status,
                coverage=coverage,
            )
        symbol = _text_or_none(stock.get("ts_code"))
        raw_exchange = _text_or_none(stock.get("exchange"))
        values = {
            "symbol": symbol,
            "name": _text_or_none(stock.get("name")),
            "exchange": _exchange_suffix(symbol or "") or raw_exchange,
        }
        coverage = build_tool_coverage(requirements, _available_fields(values))
        _raise_for_missing_required("stock.resolve_company", coverage)
        return CompanyResolution(
            query=query,
            symbol=symbol or "",
            name=values["name"] if "name" in requirements.selection.requested_fields else None,
            exchange=(
                values["exchange"]
                if "exchange" in requirements.selection.requested_fields
                else None
            ),
            source_provider=SOURCE_PROVIDER,
            as_of_date=self._as_of_date(),
            warnings=[
                *optional_permission_warnings(requirements),
            ],
            status=coverage.status,
            coverage=coverage,
        )

    @_sanitize_mapping_errors("stock.company_profile", selection_index=1)
    def get_company_profile(
        self,
        symbol: str,
        selection: FieldSelection | None = None,
    ) -> CompanyProfile:
        capability = "stock.company_profile"
        requirements = self._requirements(capability, selection)
        stock = self._stock_by_symbol(symbol) if "stock_basic" in requirements.api_fields else None
        company = None
        if "stock_company" in requirements.api_fields:
            company = self._first(
                "stock_company",
                params={"ts_code": symbol},
                fields=self._raw_fields(
                    capability,
                    "stock_company",
                    requirements.api_fields["stock_company"],
                ),
            )
        stock = stock or {}
        company = company or {}
        values: dict[str, Any] = {
            "name": _text_or_none(stock.get("name")),
            "industry": _text_or_none(stock.get("industry")),
            "board": _text_or_none(stock.get("market")),
            "main_business": _first_nonempty_text(
                company.get("main_business"), company.get("introduction")
            ),
            "registered_address": _join_text(company.get("province"), company.get("city")),
        }
        coverage = build_tool_coverage(requirements, _available_fields(values))
        _raise_for_missing_required(capability, coverage)
        warnings = optional_permission_warnings(requirements)
        if requirements.api_fields and not _available_fields(values):
            warnings.append("tushare_symbol_not_found")
        return CompanyProfile(
            symbol=symbol,
            name=_requested_value("name", values, requirements),
            industry=_requested_value("industry", values, requirements),
            board=_requested_value("board", values, requirements),
            main_business=_requested_value("main_business", values, requirements),
            registered_address=_requested_value("registered_address", values, requirements),
            controller=None,
            source_provider=SOURCE_PROVIDER,
            as_of_date=self._as_of_date(),
            warnings=warnings,
            status=coverage.status,
            coverage=coverage,
        )

    @_sanitize_mapping_errors("stock.market_snapshot", selection_index=1)
    def get_market_snapshot(
        self,
        symbol: str,
        selection: FieldSelection | None = None,
    ) -> MarketSnapshot:
        capability = "stock.market_snapshot"
        requirements = self._requirements(capability, selection)
        daily = None
        if "daily" in requirements.api_fields:
            daily = self._first(
                "daily",
                params={"ts_code": symbol},
                fields=self._raw_fields(
                    capability, "daily", requirements.api_fields["daily"]
                ),
            )
        daily_basic = None
        if "daily_basic" in requirements.api_fields:
            daily_basic = self._first(
                "daily_basic",
                params={"ts_code": symbol},
                fields=self._raw_fields(
                    capability,
                    "daily_basic",
                    requirements.api_fields["daily_basic"],
                ),
            )
        daily = daily or {}
        daily_basic = daily_basic or {}
        values = {
            "price": _float_or_none(daily.get("close")),
            "market_cap": _multiply(_float_or_none(daily_basic.get("total_mv")), 10_000),
            "pe_ttm": _float_or_none(daily_basic.get("pe_ttm")),
            "pb": _float_or_none(daily_basic.get("pb")),
            "pct_change": _float_or_none(daily.get("pct_chg")),
            "turnover": _multiply(_float_or_none(daily.get("amount")), 1_000),
        }
        coverage = build_tool_coverage(requirements, _available_fields(values))
        _raise_for_missing_required(capability, coverage)
        warnings = optional_permission_warnings(requirements)
        if requirements.api_fields and not _available_fields(values):
            warnings.append("tushare_market_snapshot_not_found")
        return MarketSnapshot(
            symbol=symbol,
            price=_requested_value("price", values, requirements),
            market_cap=_requested_value("market_cap", values, requirements),
            pe_ttm=_requested_value("pe_ttm", values, requirements),
            pb=_requested_value("pb", values, requirements),
            pct_change=_requested_value("pct_change", values, requirements),
            turnover=_requested_value("turnover", values, requirements),
            source_provider=SOURCE_PROVIDER,
            as_of_date=_format_trade_date(
                daily.get("trade_date") or daily_basic.get("trade_date") or self._as_of_date()
            ),
            warnings=warnings,
            status=coverage.status,
            coverage=coverage,
        )

    @_sanitize_mapping_errors("stock.price_trend", selection_index=2)
    def get_price_trend(
        self,
        symbol: str,
        window_days: int,
        selection: FieldSelection | None = None,
    ) -> PriceTrend:
        capability = "stock.price_trend"
        requirements = self._requirements(capability, selection)
        bounded_window = max(20, min(int(window_days), 250))
        rows = self._query(
            "daily",
            params={
                "ts_code": symbol,
                "start_date": (self._today() - timedelta(days=bounded_window * 2)).strftime(
                    "%Y%m%d"
                ),
                "end_date": self._today().strftime("%Y%m%d"),
            },
            fields=["ts_code", "trade_date", "close"],
        )
        ordered = sorted(
            (
                (_text_or_none(row.get("trade_date")) or "", _float_or_none(row.get("close")))
                for row in rows
            ),
            key=lambda item: item[0],
        )
        closes = [close for _, close in ordered if close is not None]
        values = _price_trend_values(closes)
        coverage = build_tool_coverage(requirements, _available_fields(values))
        _raise_for_missing_required(capability, coverage)
        return PriceTrend(
            symbol=symbol,
            window_days=bounded_window,
            latest_price=_requested_value("latest_price", values, requirements),
            return_20d=_requested_value("return_20d", values, requirements),
            return_60d=_requested_value("return_60d", values, requirements),
            ma20=_requested_value("ma20", values, requirements),
            ma60=_requested_value("ma60", values, requirements),
            volatility_20d=_requested_value("volatility_20d", values, requirements),
            max_drawdown=_requested_value("max_drawdown", values, requirements),
            source_provider=SOURCE_PROVIDER,
            as_of_date=_format_trade_date(ordered[-1][0]) if ordered else self._as_of_date(),
            warnings=([] if closes else ["tushare_price_history_not_found"]),
            status=coverage.status,
            coverage=coverage,
        )

    @_sanitize_mapping_errors("stock.financial_metrics", selection_index=2)
    def get_financial_metrics(
        self,
        symbol: str,
        period: str,
        selection: FieldSelection | None = None,
    ) -> FinancialMetrics:
        capability = "stock.financial_metrics"
        requirements = self._requirements(capability, selection)
        end_date = _end_date_for_period(period)
        rows_by_api: dict[str, dict[str, Any]] = {}
        for api_name, business_fields in requirements.api_fields.items():
            rows_by_api[api_name] = self._first(
                api_name,
                params={
                    "ts_code": symbol,
                    **({"end_date": end_date} if end_date is not None else {}),
                },
                fields=self._raw_fields(capability, api_name, business_fields),
            ) or {}
        indicator = rows_by_api.get("fina_indicator", {})
        income = rows_by_api.get("income", {})
        cashflow = rows_by_api.get("cashflow", {})
        values = {
            "revenue": _float_or_none(income.get("revenue")),
            "net_profit_parent": _float_or_none(income.get("n_income_attr_p")),
            "gross_margin": _percent_to_ratio(indicator.get("grossprofit_margin")),
            "roe": _percent_to_ratio(indicator.get("roe")),
            "operating_cash_flow": _float_or_none(cashflow.get("n_cashflow_act")),
            "revenue_yoy": _percent_to_ratio(indicator.get("or_yoy")),
            "net_profit_yoy": _percent_to_ratio(indicator.get("netprofit_yoy")),
            "operating_cash_flow_yoy": _percent_to_ratio(indicator.get("ocf_yoy")),
            "debt_to_asset": _percent_to_ratio(indicator.get("debt_to_assets")),
        }
        available_fields = _available_fields(values)
        coverage = build_tool_coverage(requirements, available_fields)
        _raise_for_missing_required(capability, coverage)
        warnings = optional_permission_warnings(requirements)
        if requirements.api_fields and not available_fields:
            warnings.append("tushare_financial_metrics_not_found")
        resolved_end_date = next(
            (
                _text_or_none(row.get("end_date"))
                for row in rows_by_api.values()
                if _text_or_none(row.get("end_date"))
            ),
            end_date,
        )
        resolved_period = (
            _period_label_from_end_date(resolved_end_date)
            if period == "latest"
            else period
        )
        return FinancialMetrics(
            symbol=symbol,
            period=resolved_period,
            metrics={
                field_name: values[field_name]
                for field_name in requirements.selection.requested_fields
                if field_name in available_fields
            },
            source_provider=SOURCE_PROVIDER,
            as_of_date=_format_trade_date(resolved_end_date or self._as_of_date()),
            warnings=warnings,
            status=coverage.status,
            coverage=coverage,
        )

    @_sanitize_mapping_errors("stock.announcement_search", selection_index=2)
    def search_announcements(
        self,
        symbol: str,
        date_range: str,
        selection: FieldSelection | None = None,
    ) -> AnnouncementSearchResult:
        capability = "stock.announcement_search"
        requirements = self._requirements(capability, selection)
        rows: list[dict[str, Any]] = []
        announcements_requested = "anns_d" in requirements.api_fields
        if announcements_requested:
            rows = self._query(
                "anns_d",
                params={
                    "ts_code": symbol,
                    "start_date": _start_date_for_range(date_range, today=self._today()),
                },
                fields=self._raw_fields(
                    capability, "anns_d", requirements.api_fields["anns_d"]
                ),
            )
        announcements = [
            {
                "document_id": (
                    f"tushare:{symbol}:{_format_trade_date(row.get('ann_date'))}:{idx}"
                ),
                "title": _text_or_none(row.get("title")) or "",
                "date": _format_trade_date(row.get("ann_date")),
                "type": "announcement",
                "url": _text_or_none(row.get("url")),
            }
            for idx, row in enumerate(rows)
        ]
        available_fields = {"announcements"} if announcements_requested else set()
        coverage = build_tool_coverage(requirements, available_fields)
        _raise_for_missing_required(capability, coverage)
        return AnnouncementSearchResult(
            symbol=symbol,
            date_range=date_range,
            announcements=announcements,
            source_provider=SOURCE_PROVIDER,
            as_of_date=self._as_of_date(),
            warnings=optional_permission_warnings(requirements),
            status=coverage.status,
            coverage=coverage,
        )

    def _stock_basic(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._stock_basic_cache_is_fresh(now):
            assert self._stock_basic_cache is not None
            return self._stock_basic_cache
        with self._stock_basic_cache_lock:
            now = time.monotonic()
            if self._stock_basic_cache_is_fresh(now):
                assert self._stock_basic_cache is not None
                return self._stock_basic_cache
            rows = self._query(
                "stock_basic",
                params={"exchange": "", "list_status": "L"},
                fields=STOCK_BASIC_FIELDS,
            )
            self._stock_basic_cache = rows
            self._stock_basic_cached_at = time.monotonic()
            return rows

    def _stock_basic_cache_is_fresh(self, now: float) -> bool:
        return (
            self._stock_basic_cache is not None
            and now - self._stock_basic_cached_at < self._stock_basic_cache_ttl_seconds
        )

    def _find_stock(self, query: str) -> dict[str, Any] | None:
        normalized = _normalize_query(query)
        rows = self._stock_basic()
        for row in rows:
            if normalized in {
                _normalize_query(row.get("ts_code")),
                _normalize_query(row.get("symbol")),
                _normalize_query(row.get("name")),
                _normalize_query(row.get("fullname")),
                _normalize_query(row.get("cnspell")),
            }:
                return row
        for row in rows:
            searchable_names = [
                _normalize_query(row.get("name")),
                _normalize_query(row.get("fullname")),
            ]
            if normalized and any(normalized in name for name in searchable_names):
                return row
        return None

    def _rank_company_candidates(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        normalized_query = _normalize_query(query)
        if len(normalized_query) < 2:
            return []
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        for row in self._stock_basic():
            name = _text_or_none(row.get("name"))
            symbol = _text_or_none(row.get("ts_code"))
            if not name or not symbol:
                continue
            normalized_names = [
                value
                for value in (
                    _normalize_stock_display_name(name),
                    _normalize_query(row.get("fullname")),
                )
                if value
            ]
            score = max(
                _company_name_similarity(normalized_query, candidate_name)
                for candidate_name in normalized_names
            )
            if score < 0.45:
                continue
            ranked.append(
                (
                    score,
                    name,
                    {
                        "symbol": symbol,
                        "name": name,
                        "score": round(score, 4),
                    },
                )
            )
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in ranked[:limit]]

    def _stock_by_symbol(self, symbol: str) -> dict[str, Any] | None:
        normalized = _normalize_query(symbol)
        for row in self._stock_basic():
            if _normalize_query(row.get("ts_code")) == normalized:
                return row
        return None

    def _first(
        self,
        api_name: str,
        *,
        params: dict[str, Any],
        fields: list[str],
    ) -> dict[str, Any] | None:
        rows = self._query(api_name, params=params, fields=fields)
        return rows[0] if rows else None

    def _query(
        self,
        api_name: str,
        *,
        params: dict[str, Any],
        fields: list[str],
    ) -> list[dict[str, Any]]:
        return self._client.query(api_name, params=params, fields=fields)

    def _requirements(
        self,
        capability: str,
        selection: FieldSelection | None,
    ) -> ApiRequirements:
        resolved_selection = selection or default_field_selection(capability)
        return resolve_api_requirements(
            capability,
            resolved_selection,
            self._enabled_api_names,
        )

    @staticmethod
    def _raw_fields(
        capability: str,
        api_name: str,
        business_fields: tuple[str, ...],
    ) -> list[str]:
        fields = ["ts_code"]
        if api_name in {"daily", "daily_basic"}:
            fields.append("trade_date")
        elif api_name in {"income", "cashflow", "fina_indicator"}:
            fields.append("end_date")
        raw_mapping = RAW_FIELDS[capability]
        for field_name in business_fields:
            fields.extend(raw_mapping[field_name])
        return list(dict.fromkeys(fields))

    def _as_of_date(self) -> str:
        return self._today().isoformat()


def _normalize_query(value: Any) -> str:
    return (_text_or_none(value) or "").upper()


def _normalize_stock_display_name(value: Any) -> str:
    normalized = _normalize_query(value)
    return re.sub(
        r"^(?:S\*ST|\*ST|SST|ST|XD|XR|DR|N|C)(?=[\u3400-\u9FFF])",
        "",
        normalized,
        count=1,
    )


def _end_date_for_period(period: str) -> str | None:
    normalized = period.strip().upper()
    if normalized == "LATEST":
        return None
    match = re.fullmatch(r"(20\d{2})(Q[1-4]|H1)?", normalized)
    if match is None:
        raise MarketDataError(
            "provider_invalid_request",
            "财务报告期格式无效。",
            details={"api_name": "financial_statements", "category": "invalid_request"},
            retryable=False,
        )
    year, suffix = match.groups()
    month_day = {
        "Q1": "0331",
        "Q2": "0630",
        "H1": "0630",
        "Q3": "0930",
        "Q4": "1231",
        None: "1231",
    }[suffix]
    return f"{year}{month_day}"


def _period_label_from_end_date(value: str | None) -> str:
    if not value or not re.fullmatch(r"20\d{6}", value):
        return "latest"
    year, month_day = value[:4], value[4:]
    suffix = {"0331": "Q1", "0630": "H1", "0930": "Q3", "1231": ""}.get(
        month_day,
        "",
    )
    return f"{year}{suffix}"


def _price_trend_values(closes: list[float]) -> dict[str, float | None]:
    latest = closes[-1] if closes else None

    def period_return(days: int) -> float | None:
        if len(closes) <= days or closes[-days - 1] == 0:
            return None
        return closes[-1] / closes[-days - 1] - 1

    def moving_average(days: int) -> float | None:
        if len(closes) < days:
            return None
        return sum(closes[-days:]) / days

    daily_returns = [
        current / previous - 1
        for previous, current in zip(closes[-21:-1], closes[-20:], strict=False)
        if previous != 0
    ]
    volatility = pstdev(daily_returns) * sqrt(252) if len(daily_returns) >= 2 else None
    peak: float | None = None
    max_drawdown: float | None = None
    for close in closes:
        peak = close if peak is None else max(peak, close)
        if peak == 0:
            continue
        drawdown = close / peak - 1
        max_drawdown = drawdown if max_drawdown is None else min(max_drawdown, drawdown)
    return {
        "latest_price": latest,
        "return_20d": period_return(20),
        "return_60d": period_return(60),
        "ma20": moving_average(20),
        "ma60": moving_average(60),
        "volatility_20d": volatility,
        "max_drawdown": max_drawdown,
    }


def _provider_error(
    code: str,
    *,
    api_name: str,
    retryable: bool = False,
    upstream_code: Any | None = None,
    http_status: int | None = None,
) -> MarketDataError:
    details: dict[str, Any] = {
        "api_name": api_name,
        "category": code.removeprefix("provider_"),
    }
    if upstream_code is not None:
        details["upstream_code"] = _safe_upstream_code(upstream_code)
    if http_status is not None:
        details["http_status"] = http_status
    return MarketDataError(
        code=code,
        message=PROVIDER_ERROR_MESSAGES[code],
        details=details,
        retryable=retryable,
    )


def _http_status_error(api_name: str, status_code: int) -> MarketDataError:
    if status_code == 408:
        code, retryable = "provider_timeout", True
    elif status_code == 429:
        code, retryable = "provider_rate_limited", True
    elif status_code >= 500:
        code, retryable = "provider_unavailable", True
    elif status_code in {401, 403}:
        code, retryable = "provider_authentication_failed", False
    else:
        code, retryable = "provider_invalid_request", False
    return _provider_error(
        code,
        api_name=api_name,
        retryable=retryable,
        http_status=status_code,
    )


def _business_error(
    api_name: str,
    *,
    upstream_code: Any,
    upstream_message: Any,
) -> MarketDataError:
    message = str(upstream_message or "").lower()
    if _matches_any(
        message,
        (
            "权限",
            "未开通",
            "无权",
            "没有访问",
            "permission denied",
            "not authorized",
            "access denied",
            "forbidden",
        ),
    ):
        code, retryable = "provider_permission_denied", False
    elif _matches_any(
        message,
        (
            "token 无效",
            "token无效",
            "token 失效",
            "token失效",
            "token 错误",
            "token错误",
            "token 不正确",
            "token不正确",
            "认证失败",
            "身份验证失败",
            "unauthorized",
            "authentication failed",
            "invalid token",
            "token invalid",
            "token expired",
            "token",
        ),
    ):
        code, retryable = "provider_authentication_failed", False
    elif _matches_any(
        message,
        (
            "频率",
            "每分钟",
            "每小时",
            "每天最多",
            "访问次数",
            "限流",
            "rate limit",
            "too many requests",
            "quota exceeded",
        ),
    ):
        code, retryable = "provider_rate_limited", True
    elif _matches_any(
        message,
        (
            "参数",
            "入参",
            "字段错误",
            "格式错误",
            "invalid parameter",
            "invalid argument",
            "parameter error",
            "argument error",
        ),
    ):
        code, retryable = "provider_invalid_request", False
    else:
        code, retryable = "provider_upstream_error", False
    return _provider_error(
        code,
        api_name=api_name,
        retryable=retryable,
        upstream_code=upstream_code,
    )


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in value for pattern in patterns)


def _safe_upstream_code(value: Any) -> int | str:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if re.fullmatch(r"-?\d+", text):
        return text[:32]
    return "unknown"


def _raise_for_missing_required(
    capability: str,
    coverage: ToolCoverage,
    *,
    extra_details: dict[str, Any] | None = None,
) -> None:
    if not coverage.missing_required_fields:
        return
    raise MarketDataError(
        code="required_data_missing",
        message="市场数据源未返回完成请求所需的必需字段。",
        details={
            "capability": capability,
            "missing_fields": list(coverage.missing_required_fields),
            "source_apis": list(coverage.source_apis),
            **(extra_details or {}),
        },
        retryable=False,
    )


def _company_name_similarity(query: str, candidate: str) -> float:
    sequence_score = SequenceMatcher(None, query, candidate).ratio()
    prefix_length = 0
    for query_char, candidate_char in zip(query, candidate, strict=False):
        if query_char != candidate_char:
            break
        prefix_length += 1
    prefix_score = prefix_length / max(1, min(len(query), len(candidate)))
    containment_score = 1.0 if query in candidate or candidate in query else 0.0
    return max(sequence_score, prefix_score, containment_score)


def _available_fields(values: dict[str, Any]) -> set[str]:
    return {
        field_name
        for field_name, value in values.items()
        if value is not None and value != ""
    }


def _requested_value(
    field_name: str,
    values: dict[str, Any],
    requirements: ApiRequirements,
) -> Any:
    if field_name not in requirements.selection.requested_fields:
        return None
    return values.get(field_name)


def _exchange_suffix(symbol: str) -> str:
    if "." not in symbol:
        return ""
    return symbol.split(".")[-1]


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise _UpstreamFieldMappingError from None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise _UpstreamFieldMappingError from None


def _multiply(value: float | None, multiplier: int) -> float | None:
    if value is None:
        return None
    return value * multiplier


def _percent_to_ratio(value: Any) -> float | None:
    number = _float_or_none(value)
    if number is None:
        return None
    return number / 100


def _text_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _UpstreamFieldMappingError from None
    text = value.strip()
    return text or None


def _first_nonempty_text(*values: Any) -> str | None:
    for value in values:
        text = _text_or_none(value)
        if text is not None:
            return text
    return None


def _join_text(*values: Any) -> str | None:
    text = "".join(part for value in values if (part := _text_or_none(value)))
    return text or None


def _format_trade_date(value: Any) -> str:
    text = _text_or_none(value) or ""
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _start_date_for_range(date_range: str, *, today: date) -> str:
    match = re.fullmatch(r"(\d+)m", date_range.strip().lower())
    if match:
        start = today - timedelta(days=int(match.group(1)) * 31)
    else:
        start = today - timedelta(days=365)
    return start.strftime("%Y%m%d")
