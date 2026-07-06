from typing import Protocol

from app.market_data.schemas import (
    AnnouncementSearchResult,
    CompanyProfile,
    CompanyResolution,
    FinancialMetrics,
    MarketSnapshot,
    RiskEvents,
)

FIXTURE_AS_OF_DATE = "2025-04-03"
SOURCE_PROVIDER = "fixture"


class MarketDataProvider(Protocol):
    def resolve_company(self, query: str) -> CompanyResolution: ...

    def get_company_profile(self, symbol: str) -> CompanyProfile: ...

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot: ...

    def get_financial_metrics(self, symbol: str, period: str) -> FinancialMetrics: ...

    def search_announcements(self, symbol: str, date_range: str) -> AnnouncementSearchResult: ...

    def get_risk_events(self, symbol: str, date_range: str) -> RiskEvents: ...


ALIASES = {
    "贵州茅台": "600519.SH",
    "茅台": "600519.SH",
    "600519": "600519.SH",
    "600519.SH": "600519.SH",
    "宁德时代": "300750.SZ",
    "300750": "300750.SZ",
    "300750.SZ": "300750.SZ",
    "隆基绿能": "601012.SH",
    "601012": "601012.SH",
    "601012.SH": "601012.SH",
}

PROFILES = {
    "600519.SH": {
        "name": "贵州茅台",
        "industry": "食品饮料",
        "board": "上交所主板",
        "main_business": "茅台酒及系列酒的生产与销售。",
        "registered_address": "贵州省仁怀市茅台镇",
        "controller": "贵州省国资委",
    },
    "300750.SZ": {
        "name": "宁德时代",
        "industry": "电力设备",
        "board": "深交所创业板",
        "main_business": "动力电池和储能电池系统研发、生产和销售。",
        "registered_address": "福建省宁德市",
        "controller": None,
    },
    "601012.SH": {
        "name": "隆基绿能",
        "industry": "电力设备",
        "board": "上交所主板",
        "main_business": "单晶硅片、电池组件及光伏解决方案。",
        "registered_address": "陕西省西安市",
        "controller": None,
    },
}

MARKET_SNAPSHOTS = {
    "600519.SH": {
        "price": 1523.0,
        "market_cap": 1913000000000.0,
        "pe_ttm": 25.6,
        "pb": 8.2,
        "pct_change": 0.4,
        "turnover": 4210000000.0,
    },
    "300750.SZ": {
        "price": 247.5,
        "market_cap": 1089000000000.0,
        "pe_ttm": 24.7,
        "pb": 5.8,
        "pct_change": -0.8,
        "turnover": 5280000000.0,
    },
    "601012.SH": {
        "price": 18.6,
        "market_cap": 141000000000.0,
        "pe_ttm": 17.4,
        "pb": 1.7,
        "pct_change": -1.2,
        "turnover": 1760000000.0,
    },
}

FINANCIALS = {
    ("600519.SH", "2024"): {
        "revenue": 150560000000,
        "net_profit_parent": 74734000000,
        "gross_margin": 0.918,
        "roe": 0.342,
        "operating_cash_flow": 66800000000,
        "debt_to_asset": 0.155,
    },
    ("300750.SZ", "2024"): {
        "revenue": 362010000000,
        "net_profit_parent": 44121000000,
        "gross_margin": 0.263,
        "roe": 0.247,
        "operating_cash_flow": 92800000000,
        "debt_to_asset": 0.652,
    },
}

RISK_EVENTS = {
    "601012.SH": [
        {
            "event_type": "performance_pressure",
            "date": "2025-03-20",
            "summary": "光伏产业链价格下行导致盈利承压。",
            "severity": "medium",
        }
    ]
}


class FixtureMarketDataProvider:
    def resolve_company(self, query: str) -> CompanyResolution:
        symbol = ALIASES.get(query)
        if symbol is None:
            return CompanyResolution(
                query=query,
                symbol="",
                name="",
                exchange="",
                source_provider=SOURCE_PROVIDER,
                as_of_date=FIXTURE_AS_OF_DATE,
                warnings=["fixture_company_not_found"],
            )

        profile = PROFILES[symbol]
        return CompanyResolution(
            query=query,
            symbol=symbol,
            name=profile["name"],
            exchange=symbol.split(".")[-1],
            source_provider=SOURCE_PROVIDER,
            as_of_date=FIXTURE_AS_OF_DATE,
        )

    def get_company_profile(self, symbol: str) -> CompanyProfile:
        profile = PROFILES.get(symbol)
        if profile is None:
            return CompanyProfile(
                symbol=symbol,
                name="",
                industry="",
                board="",
                main_business="",
                source_provider=SOURCE_PROVIDER,
                as_of_date=FIXTURE_AS_OF_DATE,
                warnings=["fixture_symbol_not_found"],
            )

        return CompanyProfile(
            symbol=symbol,
            source_provider=SOURCE_PROVIDER,
            as_of_date=FIXTURE_AS_OF_DATE,
            **profile,
        )

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        snapshot = MARKET_SNAPSHOTS.get(symbol)
        if snapshot is None:
            return MarketSnapshot(
                symbol=symbol,
                source_provider=SOURCE_PROVIDER,
                as_of_date=FIXTURE_AS_OF_DATE,
                warnings=["fixture_symbol_not_found"],
            )

        return MarketSnapshot(
            symbol=symbol,
            source_provider=SOURCE_PROVIDER,
            as_of_date=FIXTURE_AS_OF_DATE,
            **snapshot,
        )

    def get_financial_metrics(self, symbol: str, period: str) -> FinancialMetrics:
        metrics = FINANCIALS.get((symbol, period))
        if metrics is None:
            return FinancialMetrics(
                symbol=symbol,
                period=period,
                metrics={},
                source_provider=SOURCE_PROVIDER,
                as_of_date=FIXTURE_AS_OF_DATE,
                warnings=["fixture_financial_metrics_not_found"],
            )

        return FinancialMetrics(
            symbol=symbol,
            period=period,
            metrics=metrics,
            source_provider=SOURCE_PROVIDER,
            as_of_date=FIXTURE_AS_OF_DATE,
        )

    def search_announcements(self, symbol: str, date_range: str) -> AnnouncementSearchResult:
        if symbol not in PROFILES:
            return AnnouncementSearchResult(
                symbol=symbol,
                date_range=date_range,
                announcements=[],
                source_provider=SOURCE_PROVIDER,
                as_of_date=FIXTURE_AS_OF_DATE,
                warnings=["fixture_announcements_not_found"],
            )

        return AnnouncementSearchResult(
            symbol=symbol,
            date_range=date_range,
            announcements=[
                {
                    "document_id": f"announcement:{symbol}:2024-annual",
                    "title": f"{PROFILES[symbol]['name']}2024年年度报告",
                    "date": FIXTURE_AS_OF_DATE,
                    "type": "annual_report",
                }
            ],
            source_provider=SOURCE_PROVIDER,
            as_of_date=FIXTURE_AS_OF_DATE,
        )

    def get_risk_events(self, symbol: str, date_range: str) -> RiskEvents:
        if symbol not in PROFILES:
            return RiskEvents(
                symbol=symbol,
                date_range=date_range,
                events=[],
                source_provider=SOURCE_PROVIDER,
                as_of_date=FIXTURE_AS_OF_DATE,
                warnings=["fixture_symbol_not_found"],
            )

        return RiskEvents(
            symbol=symbol,
            date_range=date_range,
            events=RISK_EVENTS.get(symbol, []),
            source_provider=SOURCE_PROVIDER,
            as_of_date=FIXTURE_AS_OF_DATE,
        )
