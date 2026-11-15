import inspect
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from app.market_data import config
from app.market_data.capabilities import all_market_data_api_names, resolve_api_requirements
from app.market_data.config import (
    MarketDataProviderSettings,
    build_market_data_provider,
    load_market_data_provider_settings,
)
from app.market_data.provider import MarketDataError, UnavailableMarketDataProvider
from app.market_data.schemas import FieldSelection, MarketDataResult
from app.market_data.tushare_provider import TushareHttpClient, TushareMarketDataProvider
from pydantic import BaseModel, ValidationError
import pytest


class FakeTushareClient:
    def __init__(self):
        self.calls = []

    def query(self, api_name: str, *, params: dict, fields: list[str]):
        self.calls.append((api_name, params, fields))
        if api_name == "stock_basic":
            return [
                {
                    "ts_code": "300085.SZ",
                    "symbol": "300085",
                    "name": "银之杰",
                    "fullname": "深圳市银之杰科技股份有限公司",
                    "cnspell": "YZJ",
                    "area": "深圳",
                    "industry": "软件服务",
                    "market": "创业板",
                    "exchange": "SZSE",
                }
            ]
        if api_name == "stock_company":
            return [
                {
                    "ts_code": "300085.SZ",
                    "chairman": "陈向军",
                    "manager": "李军",
                    "reg_capital": 70664.0535,
                    "setup_date": "19981028",
                    "province": "广东",
                    "city": "深圳",
                    "introduction": "金融信息化服务商。",
                    "main_business": "金融软件、移动商务服务及数据中心服务。",
                    "employees": 2121,
                }
            ]
        if api_name == "daily":
            return [
                {
                    "ts_code": "300085.SZ",
                    "trade_date": "20260724",
                    "close": 18.23,
                    "pct_chg": 2.13,
                    "amount": 93210.52,
                }
            ]
        if api_name == "daily_basic":
            return [
                {
                    "ts_code": "300085.SZ",
                    "trade_date": "20260724",
                    "turnover_rate": 5.42,
                    "pe_ttm": 91.2,
                    "pb": 5.3,
                    "total_mv": 1288200.0,
                }
            ]
        if api_name == "fina_indicator":
            return [
                {
                    "ts_code": "300085.SZ",
                    "end_date": "20241231",
                    "roe": 2.41,
                    "grossprofit_margin": 38.5,
                    "debt_to_assets": 42.7,
                    "or_yoy": 12.4,
                    "netprofit_yoy": -8.6,
                    "ocf_yoy": 21.3,
                }
            ]
        if api_name == "income":
            return [
                {
                    "ts_code": "300085.SZ",
                    "end_date": "20241231",
                    "revenue": 1122334455,
                    "n_income_attr_p": 55667788,
                }
            ]
        if api_name == "cashflow":
            return [
                {
                    "ts_code": "300085.SZ",
                    "end_date": "20241231",
                    "n_cashflow_act": 33221100,
                }
            ]
        if api_name == "anns_d":
            return [
                {
                    "ts_code": "300085.SZ",
                    "ann_date": "20260701",
                    "title": "银之杰：2025年年度报告",
                    "url": "https://example.test/ann.pdf",
                }
            ]
        return []


class FakeHttpResponse:
    def __init__(self, *, status_code=200, body=None, json_error=None):
        self.status_code = status_code
        self._body = body
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._body


@pytest.mark.parametrize(
    ("upstream_message", "expected_code", "retryable"),
    [
        ("抱歉，您没有访问该接口的权限", "provider_permission_denied", False),
        ("permission denied for this endpoint", "provider_permission_denied", False),
        ("client is not authorized for this endpoint", "provider_permission_denied", False),
        ("forbidden: token is not entitled", "provider_permission_denied", False),
        ("每分钟最多访问 10 次，请稍后再试", "provider_rate_limited", True),
        ("too many requests", "provider_rate_limited", True),
        ("quota exceeded", "provider_rate_limited", True),
        ("TOKEN 无效，请检查认证信息", "provider_authentication_failed", False),
        ("unauthorized", "provider_authentication_failed", False),
        ("authentication failed", "provider_authentication_failed", False),
        ("invalid token", "provider_authentication_failed", False),
        ("参数 trade_date 格式错误", "provider_invalid_request", False),
        ("invalid parameter: trade_date", "provider_invalid_request", False),
        ("invalid argument: fields", "provider_invalid_request", False),
        ("上游内部业务校验失败 secret-payload", "provider_upstream_error", False),
    ],
)
def test_tushare_business_errors_are_classified_and_sanitized(
    monkeypatch,
    upstream_message,
    expected_code,
    retryable,
):
    monkeypatch.setattr(
        "app.market_data.tushare_provider.httpx.post",
        lambda *args, **kwargs: FakeHttpResponse(
            body={"code": -2001, "msg": upstream_message, "data": None}
        ),
    )
    client = TushareHttpClient(token="top-secret-token")

    with pytest.raises(MarketDataError) as exc_info:
        client.query("daily", params={"ts_code": "300085.SZ"}, fields=["close"])

    error = exc_info.value
    assert error.code == expected_code
    assert error.retryable is retryable
    assert error.details == {
        "api_name": "daily",
        "category": expected_code.removeprefix("provider_"),
        "upstream_code": -2001,
    }
    serialized = f"{error!r} {error} {error.message} {error.details}"
    assert upstream_message not in serialized
    assert "secret-payload" not in serialized
    assert "top-secret-token" not in serialized
    assert error.__cause__ is None


@pytest.mark.parametrize(
    ("status_code", "expected_code", "retryable"),
    [
        (429, "provider_rate_limited", True),
        (408, "provider_timeout", True),
        (500, "provider_unavailable", True),
        (503, "provider_unavailable", True),
        (401, "provider_authentication_failed", False),
        (403, "provider_authentication_failed", False),
        (400, "provider_invalid_request", False),
        (404, "provider_invalid_request", False),
    ],
)
def test_tushare_http_status_errors_are_classified(
    monkeypatch,
    status_code,
    expected_code,
    retryable,
):
    monkeypatch.setattr(
        "app.market_data.tushare_provider.httpx.post",
        lambda *args, **kwargs: FakeHttpResponse(status_code=status_code),
    )
    client = TushareHttpClient(token="token")

    with pytest.raises(MarketDataError) as exc_info:
        client.query("daily", params={}, fields=["close"])

    assert exc_info.value.code == expected_code
    assert exc_info.value.retryable is retryable
    assert exc_info.value.details == {
        "api_name": "daily",
        "category": expected_code.removeprefix("provider_"),
        "http_status": status_code,
    }
    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize(
    ("transport_error", "expected_code"),
    [
        (httpx.TimeoutException("secret timeout detail"), "provider_timeout"),
        (httpx.ConnectError("secret connection detail"), "provider_unavailable"),
        (httpx.RequestError("secret request detail"), "provider_unavailable"),
    ],
)
def test_tushare_transport_errors_are_classified_and_sanitized(
    monkeypatch,
    transport_error,
    expected_code,
):
    def raise_transport_error(*args, **kwargs):
        raise transport_error

    monkeypatch.setattr(
        "app.market_data.tushare_provider.httpx.post", raise_transport_error
    )
    client = TushareHttpClient(token="top-secret-token")

    with pytest.raises(MarketDataError) as exc_info:
        client.query("daily", params={}, fields=["close"])

    error = exc_info.value
    assert error.code == expected_code
    assert error.retryable is True
    assert error.details == {
        "api_name": "daily",
        "category": expected_code.removeprefix("provider_"),
    }
    serialized = f"{error!r} {error} {error.details}"
    assert "secret" not in serialized
    assert "top-secret-token" not in serialized
    assert error.__cause__ is None


@pytest.mark.parametrize(
    "response",
    [
        FakeHttpResponse(json_error=ValueError("secret invalid json")),
        FakeHttpResponse(body=["not", "an", "object"]),
        FakeHttpResponse(body={"code": ["invalid"], "data": {}}),
        FakeHttpResponse(body={"code": 0, "data": {"fields": "close", "items": []}}),
        FakeHttpResponse(body={"code": 0, "data": {"fields": ["close"], "items": {}}}),
    ],
)
def test_tushare_invalid_responses_are_sanitized(monkeypatch, response):
    monkeypatch.setattr(
        "app.market_data.tushare_provider.httpx.post",
        lambda *args, **kwargs: response,
    )
    client = TushareHttpClient(token="top-secret-token")

    with pytest.raises(MarketDataError) as exc_info:
        client.query("daily", params={}, fields=["close"])

    error = exc_info.value
    assert error.code == "provider_response_invalid"
    assert error.retryable is False
    assert error.details == {
        "api_name": "daily",
        "category": "response_invalid",
    }
    serialized = f"{error!r} {error} {error.details}"
    assert "secret" not in serialized
    assert "top-secret-token" not in serialized
    assert error.__cause__ is None


def test_tushare_provider_resolves_company_and_maps_profile():
    provider = TushareMarketDataProvider(
        client=FakeTushareClient(),
        token="token",
        enabled_api_names=all_market_data_api_names(),
    )

    resolution = provider.resolve_company("银之杰")
    profile = provider.get_company_profile("300085.SZ")

    assert resolution.symbol == "300085.SZ"
    assert resolution.name == "银之杰"
    assert resolution.exchange == "SZ"
    assert resolution.source_provider == "tushare"
    assert profile.industry == "软件服务"
    assert profile.board == "创业板"
    assert profile.main_business == "金融软件、移动商务服务及数据中心服务。"


def test_tushare_provider_resolves_company_by_fullname_and_cnspell():
    provider = TushareMarketDataProvider(
        client=FakeTushareClient(),
        token="token",
        enabled_api_names=all_market_data_api_names(),
    )

    fullname_resolution = provider.resolve_company("深圳市银之杰科技股份有限公司")
    spell_resolution = provider.resolve_company("YZJ")

    assert fullname_resolution.symbol == "300085.SZ"
    assert spell_resolution.symbol == "300085.SZ"


def test_tushare_provider_does_not_use_historical_name_mapping():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        client=client,
        token="token",
        enabled_api_names=all_market_data_api_names(),
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.resolve_company("银杰科技")

    assert exc_info.value.code == "required_data_missing"
    assert "namechange" not in all_market_data_api_names()
    assert all(call[0] != "namechange" for call in client.calls)


def test_tushare_provider_combines_daily_snapshot_and_financial_metrics():
    provider = TushareMarketDataProvider(
        client=FakeTushareClient(),
        token="token",
        enabled_api_names=all_market_data_api_names(),
    )

    snapshot = provider.get_market_snapshot("300085.SZ")
    metrics = provider.get_financial_metrics("300085.SZ", "2024")

    assert snapshot.price == 18.23
    assert snapshot.market_cap == 12882000000.0
    assert snapshot.pe_ttm == 91.2
    assert snapshot.turnover == 93210520.0
    assert metrics.period == "2024"
    assert metrics.metrics["revenue"] == 1122334455
    assert metrics.metrics["net_profit_parent"] == 55667788
    assert metrics.metrics["gross_margin"] == 0.385
    assert metrics.metrics["roe"] == 0.0241
    assert metrics.metrics["operating_cash_flow"] == 33221100
    assert metrics.metrics["debt_to_asset"] == pytest.approx(0.427)
    assert metrics.metrics["revenue_yoy"] == pytest.approx(0.124)
    assert metrics.metrics["net_profit_yoy"] == pytest.approx(-0.086)
    assert metrics.metrics["operating_cash_flow_yoy"] == pytest.approx(0.213)


def test_tushare_provider_uses_latest_available_financial_period():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        client=client,
        token="token",
        enabled_api_names={"income", "cashflow", "fina_indicator"},
    )

    metrics = provider.get_financial_metrics("300085.SZ", "latest")

    assert metrics.period == "2024"
    financial_calls = [call for call in client.calls if call[0] != "stock_basic"]
    assert financial_calls
    assert all("end_date" not in params for _, params, _ in financial_calls)


def test_tushare_provider_calculates_price_trend_from_daily_history():
    class PriceHistoryClient(FakeTushareClient):
        def query(self, api_name: str, *, params: dict, fields: list[str]):
            if api_name != "daily":
                return super().query(api_name, params=params, fields=fields)
            self.calls.append((api_name, params, fields))
            return [
                {
                    "ts_code": "300085.SZ",
                    "trade_date": f"202607{day:02d}",
                    "close": float(day),
                }
                for day in range(30, 0, -1)
            ]

    provider = TushareMarketDataProvider(
        client=PriceHistoryClient(),
        token="token",
        enabled_api_names={"daily"},
    )

    trend = provider.get_price_trend(
        "300085.SZ",
        30,
        FieldSelection(
            required_fields=["latest_price"],
            optional_fields=["return_20d", "ma20", "volatility_20d", "max_drawdown"],
        ),
    )

    assert trend.latest_price == 30.0
    assert trend.return_20d == pytest.approx(30 / 10 - 1)
    assert trend.ma20 == pytest.approx(sum(range(11, 31)) / 20)
    assert trend.volatility_20d is not None
    assert trend.max_drawdown == 0.0
    assert trend.coverage.source_apis == ["daily"]


def test_tushare_provider_requires_an_explicit_permission_snapshot():
    parameter = inspect.signature(TushareMarketDataProvider).parameters["enabled_api_names"]

    assert parameter.default is inspect.Parameter.empty


def test_resolve_company_uses_only_stock_basic_and_serializes_provided_fields():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"stock_basic"},
    )

    result = provider.resolve_company(
        "银之杰",
        FieldSelection(required_fields=["name"]),
    )
    payload = result.model_dump()

    assert [call[0] for call in client.calls] == ["stock_basic"]
    assert payload["symbol"] == "300085.SZ"
    assert payload["name"] == "银之杰"
    assert "exchange" not in payload
    assert result.status == "complete"
    assert result.coverage.requested_fields == ["name"]
    assert result.coverage.provided_fields == ["name"]
    assert result.coverage.source_apis == ["stock_basic"]


def test_resolve_company_blocks_missing_required_permission_without_upstream_call():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"daily"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.resolve_company(
            "银之杰",
            FieldSelection(required_fields=["symbol"]),
        )

    assert exc_info.value.code == "required_api_permission_missing"
    assert exc_info.value.details == {
        "capability": "stock.resolve_company",
        "missing_fields": ["symbol"],
        "required_apis": ["stock_basic"],
    }
    assert client.calls == []


def test_company_profile_selects_stock_basic_and_reports_optional_company_fields():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"stock_basic"},
    )

    result = provider.get_company_profile(
        "300085.SZ",
        FieldSelection(required_fields=["industry"], optional_fields=["main_business"]),
    )
    payload = result.model_dump()

    assert [call[0] for call in client.calls] == ["stock_basic"]
    assert payload["symbol"] == "300085.SZ"
    assert payload["industry"] == "软件服务"
    assert "name" not in payload
    assert "board" not in payload
    assert "main_business" not in payload
    assert "registered_address" not in payload
    assert result.status == "partial"
    assert result.coverage.provided_fields == ["industry"]
    assert result.coverage.missing_optional_fields == ["main_business"]
    assert result.coverage.source_apis == ["stock_basic"]


def test_company_profile_selects_only_stock_company_for_business_details():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"stock_company"},
    )

    result = provider.get_company_profile(
        "300085.SZ",
        FieldSelection(required_fields=["main_business"]),
    )
    payload = result.model_dump()

    assert [call[0] for call in client.calls] == ["stock_company"]
    assert payload["main_business"] == "金融软件、移动商务服务及数据中心服务。"
    assert "name" not in payload
    assert "industry" not in payload
    assert "board" not in payload
    assert result.coverage.source_apis == ["stock_company"]


def test_company_profile_blocks_missing_required_permission_without_upstream_call():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"stock_basic"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_company_profile(
            "300085.SZ",
            FieldSelection(required_fields=["registered_address"]),
        )

    assert exc_info.value.code == "required_api_permission_missing"
    assert client.calls == []


def test_announcement_search_calls_only_anns_d_and_reports_complete_coverage():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"anns_d"},
    )

    result = provider.search_announcements(
        "300085.SZ",
        "12m",
        FieldSelection(required_fields=["announcements"]),
    )
    payload = result.model_dump()

    assert [call[0] for call in client.calls] == ["anns_d"]
    assert len(payload["announcements"]) == 1
    assert payload["announcements"][0]["title"] == "银之杰：2025年年度报告"
    assert result.status == "complete"
    assert result.coverage.requested_fields == ["announcements"]
    assert result.coverage.provided_fields == ["announcements"]
    assert result.coverage.source_apis == ["anns_d"]


def test_announcement_search_serializes_empty_list_after_successful_api_call():
    class EmptyAnnouncementClient(FakeTushareClient):
        def query(self, api_name: str, *, params: dict, fields: list[str]):
            if api_name == "anns_d":
                self.calls.append((api_name, params, fields))
                return []
            return super().query(api_name, params=params, fields=fields)

    client = EmptyAnnouncementClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"anns_d"},
    )

    result = provider.search_announcements(
        "300085.SZ",
        "12m",
        FieldSelection(required_fields=["announcements"]),
    )
    payload = result.model_dump()

    assert payload["announcements"] == []
    assert result.coverage.provided_fields == ["announcements"]
    assert result.status == "complete"


def test_optional_announcement_without_permission_omits_container():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"stock_basic"},
    )

    result = provider.search_announcements(
        "300085.SZ",
        "12m",
        FieldSelection(optional_fields=["announcements"]),
    )

    assert client.calls == []
    assert "announcements" not in result.model_dump()
    assert result.coverage.missing_optional_fields == ["announcements"]


def test_announcement_search_blocks_missing_required_permission_without_upstream_call():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"stock_basic"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.search_announcements(
            "300085.SZ",
            "12m",
            FieldSelection(required_fields=["announcements"]),
        )

    assert exc_info.value.code == "required_api_permission_missing"
    assert exc_info.value.details == {
        "capability": "stock.announcement_search",
        "missing_fields": ["announcements"],
        "required_apis": ["anns_d"],
    }
    assert client.calls == []


def test_field_selection_normalizes_fields_and_rejects_overlap():
    selection = FieldSelection(
        required_fields=[" price ", "price"],
        optional_fields=[" pe_ttm ", "pb", "pe_ttm"],
    )

    assert selection.required_fields == ["price"]
    assert selection.optional_fields == ["pe_ttm", "pb"]

    with pytest.raises(ValidationError, match="必需字段与可选字段不能重复"):
        FieldSelection(required_fields=["price"], optional_fields=["price"])

    with pytest.raises(ValidationError, match="至少声明一个必需或可选字段"):
        FieldSelection()


def test_market_snapshot_blocks_missing_required_api_without_calling_tushare():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"daily"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_market_snapshot(
            "300085.SZ",
            FieldSelection(required_fields=["pe_ttm"]),
        )

    assert exc_info.value.code == "required_api_permission_missing"
    assert exc_info.value.details == {
        "capability": "stock.market_snapshot",
        "missing_fields": ["pe_ttm"],
        "required_apis": ["daily_basic"],
    }
    assert exc_info.value.retryable is False
    assert client.calls == []


def test_multi_api_required_permission_failure_is_atomic_before_any_upstream_call():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"daily"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_market_snapshot(
            "300085.SZ",
            FieldSelection(required_fields=["price", "pe_ttm"]),
        )

    assert exc_info.value.code == "required_api_permission_missing"
    assert exc_info.value.details["missing_fields"] == ["pe_ttm"]
    assert client.calls == []


def test_market_snapshot_calls_only_daily_when_valuation_is_optional():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"daily"},
    )

    result = provider.get_market_snapshot(
        "300085.SZ",
        FieldSelection(required_fields=["price"], optional_fields=["pe_ttm"]),
    )

    assert [call[0] for call in client.calls] == ["daily"]
    assert result.price == 18.23
    assert result.pe_ttm is None
    assert "pe_ttm" not in result.model_dump()
    assert result.model_dump()["symbol"] == "300085.SZ"
    assert result.status == "partial"
    assert result.coverage.requested_fields == ["price", "pe_ttm"]
    assert result.coverage.provided_fields == ["price"]
    assert result.coverage.missing_required_fields == []
    assert result.coverage.missing_optional_fields == ["pe_ttm"]
    assert result.coverage.source_apis == ["daily"]


def test_market_snapshot_calls_only_api_needed_by_required_fields():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"daily", "daily_basic"},
    )

    result = provider.get_market_snapshot(
        "300085.SZ",
        FieldSelection(required_fields=["price"]),
    )

    assert [call[0] for call in client.calls] == ["daily"]
    assert result.status == "complete"
    assert result.price == 18.23
    assert result.pe_ttm is None
    payload = result.model_dump()
    assert payload["price"] == 18.23
    for field_name in ("market_cap", "pe_ttm", "pb", "pct_change", "turnover"):
        assert field_name not in payload
    assert result.coverage.provided_fields == ["price"]
    assert client.calls[0][2] == ["ts_code", "trade_date", "close"]


def test_financial_metrics_query_only_apis_needed_by_requested_fields():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"income", "cashflow", "fina_indicator"},
    )

    result = provider.get_financial_metrics(
        "300085.SZ",
        "2024",
        FieldSelection(
            required_fields=["revenue", "operating_cash_flow"],
            optional_fields=["roe"],
        ),
    )

    assert [call[0] for call in client.calls] == ["income", "cashflow", "fina_indicator"]
    assert result.metrics == {
        "revenue": 1122334455.0,
        "operating_cash_flow": 33221100.0,
        "roe": 0.0241,
    }
    assert result.model_dump()["metrics"] == {
        "revenue": 1122334455.0,
        "operating_cash_flow": 33221100.0,
        "roe": 0.0241,
    }
    assert result.status == "complete"
    assert result.coverage.source_apis == ["income", "cashflow", "fina_indicator"]


def test_optional_financial_metrics_without_permission_omit_metrics_container():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"daily"},
    )

    result = provider.get_financial_metrics(
        "300085.SZ",
        "2024",
        FieldSelection(optional_fields=["roe"]),
    )

    assert client.calls == []
    assert "metrics" not in result.model_dump()
    assert result.coverage.missing_optional_fields == ["roe"]


def test_market_data_result_preserves_subclass_fields_when_nested_in_envelope():
    class Envelope(BaseModel):
        result: MarketDataResult

    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"daily", "daily_basic"},
    )
    snapshot = provider.get_market_snapshot(
        "300085.SZ",
        FieldSelection(required_fields=["price"]),
    )

    payload = Envelope(result=snapshot).model_dump()["result"]

    assert payload["symbol"] == "300085.SZ"
    assert payload["price"] == 18.23
    assert payload["source_provider"] == "tushare"
    assert "pe_ttm" not in payload


def test_stock_basic_cold_cache_is_loaded_once_under_concurrency():
    class SlowStockBasicClient(FakeTushareClient):
        def query(self, api_name: str, *, params: dict, fields: list[str]):
            if api_name == "stock_basic":
                time.sleep(0.05)
            return super().query(api_name, params=params, fields=fields)

    client = SlowStockBasicClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"stock_basic"},
        stock_basic_cache_ttl_seconds=3600,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(provider.resolve_company, ["银之杰", "300085.SZ"]))

    assert [result.symbol for result in results] == ["300085.SZ", "300085.SZ"]
    assert [call[0] for call in client.calls] == ["stock_basic"]


def test_unknown_field_is_rejected_before_any_upstream_call():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"daily", "daily_basic"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_market_snapshot(
            "300085.SZ",
            FieldSelection(required_fields=["future_price"]),
        )

    assert exc_info.value.code == "invalid_field_request"
    assert exc_info.value.details == {
        "capability": "stock.market_snapshot",
        "unknown_fields": ["future_price"],
    }
    assert client.calls == []


def test_unknown_capability_is_rejected_as_invalid_field_request():
    with pytest.raises(MarketDataError) as exc_info:
        resolve_api_requirements(
            "stock.future_capability",
            FieldSelection(required_fields=["price"]),
            {"daily"},
        )

    assert exc_info.value.code == "invalid_field_request"
    assert exc_info.value.details == {"capability": "stock.future_capability"}


def test_missing_required_data_raises_structured_error():
    class EmptyDailyClient(FakeTushareClient):
        def query(self, api_name: str, *, params: dict, fields: list[str]):
            self.calls.append((api_name, params, fields))
            return []

    client = EmptyDailyClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"daily"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_market_snapshot(
            "300085.SZ",
            FieldSelection(required_fields=["price"]),
        )

    assert exc_info.value.code == "required_data_missing"
    assert exc_info.value.retryable is False
    assert exc_info.value.details == {
        "capability": "stock.market_snapshot",
        "missing_fields": ["price"],
        "source_apis": ["daily"],
    }


def test_malicious_numeric_value_is_sanitized_as_invalid_provider_response():
    class MaliciousDailyClient(FakeTushareClient):
        def query(self, api_name: str, *, params: dict, fields: list[str]):
            self.calls.append((api_name, params, fields))
            return [{"ts_code": "300085.SZ", "close": "token=secret"}]

    client = MaliciousDailyClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"daily"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_market_snapshot(
            "300085.SZ",
            FieldSelection(required_fields=["price"]),
        )

    error = exc_info.value
    assert error.code == "provider_response_invalid"
    assert error.retryable is False
    assert error.details == {
        "capability": "stock.market_snapshot",
        "source_apis": ["daily"],
    }
    serialized = f"{error!r} {error} {error.details}"
    assert "token=secret" not in serialized
    assert "secret" not in serialized
    assert error.__suppress_context__ is True
    assert error.__cause__ is None
    assert "secret" not in repr(error.__context__)


def test_provider_does_not_reclassify_unrelated_value_errors_as_mapping_errors():
    class BuggyClient(FakeTushareClient):
        def query(self, api_name: str, *, params: dict, fields: list[str]):
            raise ValueError("programming failure")

    provider = TushareMarketDataProvider(
        token="token",
        client=BuggyClient(),
        enabled_api_names={"daily"},
    )

    with pytest.raises(ValueError, match="programming failure"):
        provider.get_market_snapshot(
            "300085.SZ",
            FieldSelection(required_fields=["price"]),
        )


@pytest.mark.parametrize(
    ("malicious_field", "malicious_value", "query"),
    [
        ("name", {"token": "nested-name-secret"}, "银之杰"),
        ("ts_code", {"token": "nested-symbol-secret"}, "银之杰"),
        ("symbol", {"token": "nested-short-symbol-secret"}, "银之杰"),
        ("exchange", {"token": "nested-exchange-secret"}, "银之杰"),
    ],
)
def test_malicious_company_identity_text_is_never_stringified_or_leaked(
    malicious_field,
    malicious_value,
    query,
):
    class MaliciousIdentityClient(FakeTushareClient):
        def query(self, api_name: str, *, params: dict, fields: list[str]):
            row = {
                "ts_code": "300085.SZ",
                "symbol": "300085",
                "name": "银之杰",
                "exchange": "SZSE",
            }
            row[malicious_field] = malicious_value
            self.calls.append((api_name, params, fields))
            return [row]

    provider = TushareMarketDataProvider(
        token="token",
        client=MaliciousIdentityClient(),
        enabled_api_names={"stock_basic"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.resolve_company(
            query,
            FieldSelection(required_fields=["symbol", "name"]),
        )

    _assert_sanitized_mapping_error(exc_info.value, "stock.resolve_company")


@pytest.mark.parametrize("malicious_field", ["ann_date", "title", "url"])
def test_malicious_announcement_text_is_never_stringified_or_leaked(
    malicious_field,
):
    class MaliciousAnnouncementClient(FakeTushareClient):
        def query(self, api_name: str, *, params: dict, fields: list[str]):
            self.calls.append((api_name, params, fields))
            row = {
                "ann_date": "20260701",
                "title": "年度报告",
                "url": "https://example.test/ann.pdf",
            }
            row[malicious_field] = {"token": "nested-announcement-secret"}
            return [row]

    provider = TushareMarketDataProvider(
        token="token",
        client=MaliciousAnnouncementClient(),
        enabled_api_names={"anns_d"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.search_announcements(
            "300085.SZ",
            "12m",
            FieldSelection(required_fields=["announcements"]),
        )

    _assert_sanitized_mapping_error(exc_info.value, "stock.announcement_search")


@pytest.mark.parametrize(
    ("api_name", "malicious_field", "required_field"),
    [
        ("stock_basic", "industry", "industry"),
        ("stock_basic", "market", "board"),
        ("stock_company", "main_business", "main_business"),
        ("stock_company", "introduction", "main_business"),
        ("stock_company", "province", "registered_address"),
        ("stock_company", "city", "registered_address"),
    ],
)
def test_malicious_company_profile_text_is_never_stringified_or_leaked(
    api_name,
    malicious_field,
    required_field,
):
    class MaliciousProfileClient(FakeTushareClient):
        def query(self, requested_api: str, *, params: dict, fields: list[str]):
            rows = super().query(requested_api, params=params, fields=fields)
            if requested_api != api_name:
                return rows
            row = dict(rows[0])
            if malicious_field == "introduction":
                row["main_business"] = None
            row[malicious_field] = {"token": "nested-profile-secret"}
            return [row]

    provider = TushareMarketDataProvider(
        token="token",
        client=MaliciousProfileClient(),
        enabled_api_names={api_name},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_company_profile(
            "300085.SZ",
            FieldSelection(required_fields=[required_field]),
        )

    _assert_sanitized_mapping_error(exc_info.value, "stock.company_profile")


def test_company_not_found_raises_required_data_missing():
    class CompanyNotFoundClient(FakeTushareClient):
        def query(self, api_name: str, *, params: dict, fields: list[str]):
            self.calls.append((api_name, params, fields))
            return []

    client = CompanyNotFoundClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"stock_basic"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.resolve_company(
            "不存在的公司",
            FieldSelection(required_fields=["symbol", "name"]),
        )

    assert exc_info.value.code == "required_data_missing"
    assert exc_info.value.details == {
        "capability": "stock.resolve_company",
        "missing_fields": ["symbol", "name"],
        "source_apis": ["stock_basic"],
    }


def test_company_not_found_returns_ranked_stock_basic_candidates():
    class CandidateClient(FakeTushareClient):
        def query(self, api_name: str, *, params: dict, fields: list[str]):
            self.calls.append((api_name, params, fields))
            if api_name != "stock_basic":
                return []
            return [
                {
                    "ts_code": "688825.SH",
                    "symbol": "688825",
                    "name": "C长鑫",
                    "fullname": "长鑫科技集团股份有限公司",
                    "cnspell": "CXKJ",
                },
                {
                    "ts_code": "603986.SH",
                    "symbol": "603986",
                    "name": "佰维存储",
                    "fullname": "深圳佰维存储科技股份有限公司",
                    "cnspell": "BWCC",
                },
            ]

    provider = TushareMarketDataProvider(
        token="token",
        client=CandidateClient(),
        enabled_api_names={"stock_basic"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.resolve_company(
            "长鑫存储",
            FieldSelection(required_fields=["symbol", "name"]),
        )

    candidates = exc_info.value.details["candidates"]
    assert candidates[0]["symbol"] == "688825.SH"
    assert candidates[0]["name"] == "C长鑫"
    assert candidates[0]["score"] == 1.0
    assert candidates[0]["score"] - candidates[1]["score"] >= 0.1


def test_company_profile_missing_required_data_raises_structured_error():
    class EmptyCompanyClient(FakeTushareClient):
        def query(self, api_name: str, *, params: dict, fields: list[str]):
            self.calls.append((api_name, params, fields))
            return []

    client = EmptyCompanyClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"stock_company"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_company_profile(
            "300085.SZ",
            FieldSelection(required_fields=["main_business"]),
        )

    assert exc_info.value.code == "required_data_missing"
    assert exc_info.value.details == {
        "capability": "stock.company_profile",
        "missing_fields": ["main_business"],
        "source_apis": ["stock_company"],
    }


def test_financial_metrics_missing_required_data_raises_structured_error():
    class EmptyIncomeClient(FakeTushareClient):
        def query(self, api_name: str, *, params: dict, fields: list[str]):
            self.calls.append((api_name, params, fields))
            return []

    client = EmptyIncomeClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"income"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_financial_metrics(
            "300085.SZ",
            "2024",
            FieldSelection(required_fields=["revenue"]),
        )

    assert exc_info.value.code == "required_data_missing"
    assert exc_info.value.details == {
        "capability": "stock.financial_metrics",
        "missing_fields": ["revenue"],
        "source_apis": ["income"],
    }


def test_financial_multi_api_permission_failure_is_atomic_before_upstream_calls():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"income"},
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_financial_metrics(
            "300085.SZ",
            "2024",
            FieldSelection(required_fields=["revenue", "roe"]),
        )

    assert exc_info.value.code == "required_api_permission_missing"
    assert exc_info.value.details == {
        "capability": "stock.financial_metrics",
        "missing_fields": ["roe"],
        "required_apis": ["fina_indicator"],
    }
    assert client.calls == []


def test_optional_only_selection_skips_unauthorized_api_and_returns_partial():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token",
        client=client,
        enabled_api_names={"daily"},
    )

    result = provider.get_market_snapshot(
        "300085.SZ",
        FieldSelection(optional_fields=["pe_ttm"]),
    )

    assert client.calls == []
    assert result.status == "partial"
    assert result.coverage.missing_optional_fields == ["pe_ttm"]


def test_provider_factory_passes_enabled_api_names_to_tushare_provider():
    client = FakeTushareClient()
    provider = build_market_data_provider(
        MarketDataProviderSettings(
            status="configured",
            provider="tushare",
            token="token",
            enabled_api_names={"daily"},
        ),
        tushare_client=client,
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_market_snapshot(
            "300085.SZ",
            FieldSelection(required_fields=["pe_ttm"]),
        )

    assert exc_info.value.code == "required_api_permission_missing"
    assert client.calls == []


def test_market_data_provider_factory_builds_tushare_when_explicitly_configured():
    tushare = build_market_data_provider(
        MarketDataProviderSettings(
            status="configured",
            provider="tushare",
            token="token",
            enabled_api_names={"stock_basic"},
        ),
        tushare_client=FakeTushareClient(),
    )

    assert tushare.resolve_company("银之杰").symbol == "300085.SZ"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "configured", "provider": None, "token": "token", "enabled_api_names": {"daily"}},
        {"status": "configured", "provider": "tushare", "token": None, "enabled_api_names": {"daily"}},
        {"status": "configured", "provider": "tushare", "token": "token", "enabled_api_names": set()},
        {
            "status": "configured",
            "provider": "tushare",
            "token": "token",
            "enabled_api_names": {"daily"},
            "error": MarketDataError("test", "不应携带 error"),
        },
        {"status": "not_configured", "token": "token"},
        {"status": "not_configured", "provider": "tushare"},
        {"status": "not_configured", "error": MarketDataError("test", "不应携带 error")},
        {"status": "configuration_unavailable", "provider": "tushare", "token": "token"},
        {"status": "configuration_unavailable"},
    ],
)
def test_market_data_provider_settings_reject_invalid_status_combinations(payload):
    with pytest.raises(ValidationError):
        MarketDataProviderSettings(**payload)


def test_configuration_unavailable_can_retain_provider_without_token_for_diagnostics():
    settings = MarketDataProviderSettings(
        status="configuration_unavailable",
        provider="tushare",
        error=MarketDataError("market_data_configuration_invalid", "配置不完整"),
    )

    assert settings.provider == "tushare"
    assert settings.token is None


def test_market_data_provider_settings_never_exposes_token_in_repr_or_model_dump():
    settings = MarketDataProviderSettings(
        status="configured",
        provider="tushare",
        token="secret-token-value",
        enabled_api_names={"daily"},
    )

    assert "secret-token-value" not in repr(settings)
    assert "token" not in settings.model_dump()
    assert settings.token is not None
    assert settings.token.get_secret_value() == "secret-token-value"


def test_provider_factory_defensively_rejects_manually_constructed_invalid_settings():
    invalid_settings = MarketDataProviderSettings.model_construct(
        status="configured",
        provider="tushare",
        token=None,
        enabled_api_names={"daily"},
        token_env_var="TUSHARE_TOKEN",
    )

    provider = build_market_data_provider(invalid_settings)

    _assert_all_unavailable_methods_fail(
        provider,
        code="market_data_configuration_invalid",
        retryable=False,
    )


def test_no_configuration_builds_a_not_configured_unavailable_provider(monkeypatch):
    _clear_market_data_environment(monkeypatch)

    settings = load_market_data_provider_settings()
    provider = build_market_data_provider(settings)

    assert settings.status == "not_configured"
    assert settings.provider is None
    assert isinstance(provider, UnavailableMarketDataProvider)
    _assert_all_unavailable_methods_fail(
        provider,
        code="market_data_source_not_configured",
        retryable=False,
    )


def test_admin_request_failure_is_closed_and_does_not_fall_back_to_local_environment(monkeypatch):
    _clear_market_data_environment(monkeypatch)
    monkeypatch.setenv("MCP_MARKET_DATA_ADMIN_BASE_URL", "http://admin.test")
    monkeypatch.setenv("MCP_MARKET_DATA_PROVIDER", "tushare")
    monkeypatch.setenv("TUSHARE_TOKEN", "local-token-must-not-be-used")

    def failed_get(*_args, **_kwargs):
        raise config.httpx.ConnectError("admin unavailable")

    monkeypatch.setattr(config.httpx, "get", failed_get)

    settings = load_market_data_provider_settings()
    provider = build_market_data_provider(settings)

    assert settings.status == "configuration_unavailable"
    assert settings.provider is None
    assert settings.error is not None
    assert settings.error.details == {"reason": "admin_request_failed"}
    assert isinstance(provider, UnavailableMarketDataProvider)
    _assert_all_unavailable_methods_fail(
        provider,
        code="market_data_configuration_unavailable",
        retryable=True,
    )


def test_invalid_admin_response_is_closed_and_does_not_fall_back_to_local_environment(monkeypatch):
    _clear_market_data_environment(monkeypatch)
    monkeypatch.setenv("MCP_MARKET_DATA_ADMIN_BASE_URL", "http://admin.test")
    monkeypatch.setenv("MCP_MARKET_DATA_PROVIDER", "tushare")
    monkeypatch.setenv("TUSHARE_TOKEN", "local-token-must-not-be-used")

    class FakeResponse:
        status_code = 200
        headers = {"x-request-id": "req-safe-123"}

        def raise_for_status(self):
            return None

        def json(self):
            return {"provider": "tushare", "settings": {"enabled_api_names": "stock_basic"}}

    monkeypatch.setattr(config.httpx, "get", lambda *_args, **_kwargs: FakeResponse())

    settings = load_market_data_provider_settings()
    provider = build_market_data_provider(settings)

    assert settings.status == "configuration_unavailable"
    assert settings.provider is None
    assert settings.error is not None
    assert settings.error.details == {
        "reason": "admin_configuration_invalid",
        "http_status": 200,
        "request_id": "req-safe-123",
    }
    _assert_all_unavailable_methods_fail(
        provider,
        code="market_data_configuration_unavailable",
        retryable=False,
    )


def test_provider_settings_can_be_loaded_from_admin_active_config(monkeypatch):
    _clear_market_data_environment(monkeypatch)
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "tushare-prod",
                "provider": "tushare",
                "settings": {
                    "token_env_var": "BS_TUSHARE_TOKEN",
                    "base_url": "https://api.tushare.pro",
                    "timeout_seconds": 12,
                    "stock_basic_cache_ttl_seconds": 1800,
                    "enabled_api_names": ["stock_basic", "daily"],
                },
            }

    def fake_get(url: str, *, timeout: float):
        assert url == "http://admin.test/api/market-data-providers/active"
        assert timeout == 2.0
        return FakeResponse()

    monkeypatch.setenv("MCP_MARKET_DATA_ADMIN_BASE_URL", "http://admin.test")
    monkeypatch.setenv("BS_TUSHARE_TOKEN", "admin-token")
    monkeypatch.setattr(config.httpx, "get", fake_get)

    settings = load_market_data_provider_settings()

    assert settings.status == "configured"
    assert settings.provider == "tushare"
    assert settings.token is not None
    assert settings.token.get_secret_value() == "admin-token"
    assert settings.token_env_var == "BS_TUSHARE_TOKEN"
    assert settings.timeout_seconds == 12
    assert settings.stock_basic_cache_ttl_seconds == 1800
    assert settings.enabled_api_names == {"stock_basic", "daily"}


def test_admin_configuration_preserves_explicit_zero_stock_basic_cache_ttl(monkeypatch):
    _clear_market_data_environment(monkeypatch)

    class FakeResponse:
        status_code = 200
        headers = {}

        def json(self):
            return {
                "provider": "tushare",
                "settings": {
                    "token_env_var": "BS_TUSHARE_TOKEN",
                    "enabled_api_names": ["stock_basic"],
                    "stock_basic_cache_ttl_seconds": 0,
                },
            }

    monkeypatch.setenv("MCP_MARKET_DATA_ADMIN_BASE_URL", "http://admin.test")
    monkeypatch.setenv("BS_TUSHARE_TOKEN", "admin-token")
    monkeypatch.setattr(config.httpx, "get", lambda *_args, **_kwargs: FakeResponse())

    settings = load_market_data_provider_settings()

    assert settings.status == "configured"
    assert settings.stock_basic_cache_ttl_seconds == 0


def test_admin_configuration_with_missing_token_is_a_clear_configuration_error(monkeypatch):
    _clear_market_data_environment(monkeypatch)

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "provider": "tushare",
                "settings": {
                    "token_env_var": "MISSING_TUSHARE_TOKEN",
                    "enabled_api_names": ["stock_basic"],
                },
            }

    monkeypatch.setenv("MCP_MARKET_DATA_ADMIN_BASE_URL", "http://admin.test")
    monkeypatch.setattr(config.httpx, "get", lambda *_args, **_kwargs: FakeResponse())

    settings = load_market_data_provider_settings()
    provider = build_market_data_provider(settings)

    assert settings.status == "configuration_unavailable"
    assert settings.provider == "tushare"
    assert settings.token is None
    _assert_all_unavailable_methods_fail(
        provider,
        code="market_data_configuration_invalid",
        retryable=False,
    )


def test_explicit_local_tushare_configuration_is_used_only_when_complete(monkeypatch):
    _clear_market_data_environment(monkeypatch)
    monkeypatch.setenv("MCP_MARKET_DATA_PROVIDER", "tushare")
    monkeypatch.setenv("MCP_TUSHARE_TOKEN_ENV_VAR", "LOCAL_TUSHARE_TOKEN")
    monkeypatch.setenv("LOCAL_TUSHARE_TOKEN", "local-token")
    monkeypatch.setenv("MCP_TUSHARE_STOCK_BASIC_CACHE_TTL_SECONDS", "1800")
    monkeypatch.setenv("MCP_TUSHARE_ENABLED_API_NAMES", " stock_basic, daily,stock_basic ")

    settings = load_market_data_provider_settings()
    provider = build_market_data_provider(settings, tushare_client=FakeTushareClient())

    assert settings.status == "configured"
    assert settings.provider == "tushare"
    assert settings.token is not None
    assert settings.token.get_secret_value() == "local-token"
    assert settings.enabled_api_names == {"stock_basic", "daily"}
    assert isinstance(provider, TushareMarketDataProvider)


def test_explicit_local_tushare_without_token_is_configuration_error(monkeypatch):
    _clear_market_data_environment(monkeypatch)
    monkeypatch.setenv("MCP_MARKET_DATA_PROVIDER", "tushare")

    settings = load_market_data_provider_settings()
    provider = build_market_data_provider(settings)

    assert settings.status == "configuration_unavailable"
    _assert_all_unavailable_methods_fail(
        provider,
        code="market_data_configuration_invalid",
        retryable=False,
    )


def test_explicit_local_tushare_requires_enabled_api_names(monkeypatch):
    _clear_market_data_environment(monkeypatch)
    monkeypatch.setenv("MCP_MARKET_DATA_PROVIDER", "tushare")
    monkeypatch.setenv("TUSHARE_TOKEN", "local-token")

    settings = load_market_data_provider_settings()

    assert settings.status == "configuration_unavailable"
    assert settings.error is not None
    assert settings.error.code == "market_data_configuration_invalid"
    assert settings.error.details["reason"] == "enabled_api_names_missing"


@pytest.mark.parametrize(
    ("status_code", "body", "expected_status", "expected_reason", "retryable"),
    [
        (404, {"detail": "未配置激活的数据源。"}, "not_configured", None, None),
        (404, {"detail": "not found"}, "configuration_unavailable", "admin_http_status_error", False),
        (400, {"detail": "bad request"}, "configuration_unavailable", "admin_http_status_error", False),
        (401, {"detail": "unauthorized"}, "configuration_unavailable", "admin_http_status_error", False),
        (403, {"detail": "forbidden"}, "configuration_unavailable", "admin_http_status_error", False),
        (408, {"detail": "timeout"}, "configuration_unavailable", "admin_http_status_error", True),
        (429, {"detail": "too many requests"}, "configuration_unavailable", "admin_http_status_error", True),
        (500, {"detail": "server error"}, "configuration_unavailable", "admin_http_status_error", True),
    ],
)
def test_admin_http_statuses_are_classified_without_response_body_leaks(
    monkeypatch,
    status_code,
    body,
    expected_status,
    expected_reason,
    retryable,
):
    _clear_market_data_environment(monkeypatch)

    class FakeResponse:
        headers = {"x-request-id": "req-status-456"}

        def __init__(self):
            self.status_code = status_code

        def json(self):
            return body

    monkeypatch.setenv("MCP_MARKET_DATA_ADMIN_BASE_URL", "http://admin.test")
    monkeypatch.setattr(config.httpx, "get", lambda *_args, **_kwargs: FakeResponse())

    settings = load_market_data_provider_settings()

    assert settings.status == expected_status
    if expected_reason is None:
        assert settings.error is None
    else:
        assert settings.error is not None
        assert settings.error.details == {
            "reason": expected_reason,
            "http_status": status_code,
            "request_id": "req-status-456",
        }
        assert settings.error.retryable is retryable


def test_invalid_admin_json_is_configuration_unavailable_with_observable_safe_details(monkeypatch):
    _clear_market_data_environment(monkeypatch)

    class FakeResponse:
        status_code = 200
        headers = {"x-request-id": "req-json-789"}

        def json(self):
            raise ValueError("token=must-not-leak")

    monkeypatch.setenv("MCP_MARKET_DATA_ADMIN_BASE_URL", "http://admin.test")
    monkeypatch.setattr(config.httpx, "get", lambda *_args, **_kwargs: FakeResponse())

    settings = load_market_data_provider_settings()

    assert settings.status == "configuration_unavailable"
    assert settings.error is not None
    assert settings.error.details == {
        "reason": "admin_response_invalid",
        "http_status": 200,
        "request_id": "req-json-789",
    }
    assert settings.error.retryable is False


def _assert_all_unavailable_methods_fail(
    provider: UnavailableMarketDataProvider,
    *,
    code: str,
    retryable: bool,
) -> None:
    calls = [
        lambda: provider.resolve_company("银之杰"),
        lambda: provider.get_company_profile("300085.SZ"),
        lambda: provider.get_market_snapshot("300085.SZ"),
        lambda: provider.get_financial_metrics("300085.SZ", "2024"),
        lambda: provider.search_announcements("300085.SZ", "12m"),
    ]
    errors: list[MarketDataError] = []
    for call in calls:
        with pytest.raises(MarketDataError) as exc_info:
            call()
        errors.append(exc_info.value)

    assert {error.code for error in errors} == {code}
    assert {error.retryable for error in errors} == {retryable}
    assert all(error.message for error in errors)
    assert len({id(error) for error in errors}) == len(errors)
    assert len({id(error.__traceback__) for error in errors}) == len(errors)


def _assert_sanitized_mapping_error(error: MarketDataError, capability: str) -> None:
    assert error.code == "provider_response_invalid"
    assert error.details["capability"] == capability
    assert error.retryable is False
    serialized = f"{error!r} {error} {error.details} {error.__context__!r}"
    assert "secret" not in serialized
    assert "token" not in serialized.lower()
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def _clear_market_data_environment(monkeypatch) -> None:
    for name in (
        "MCP_MARKET_DATA_ADMIN_BASE_URL",
        "MCP_MARKET_DATA_PROVIDER",
        "MCP_TUSHARE_TOKEN_ENV_VAR",
        "MCP_TUSHARE_BASE_URL",
        "MCP_TUSHARE_TIMEOUT_SECONDS",
        "MCP_TUSHARE_STOCK_BASIC_CACHE_TTL_SECONDS",
        "MCP_TUSHARE_ENABLED_API_NAMES",
        "TUSHARE_TOKEN",
        "BS_TUSHARE_TOKEN",
        "LOCAL_TUSHARE_TOKEN",
        "MISSING_TUSHARE_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
