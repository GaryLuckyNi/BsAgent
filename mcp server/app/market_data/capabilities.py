from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from app.market_data.provider import MarketDataError
from app.market_data.schemas import FieldSelection, ToolCoverage


MARKET_DATA_FIELD_APIS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "stock.resolve_company": MappingProxyType(
            {
                "symbol": "stock_basic",
                "name": "stock_basic",
                "exchange": "stock_basic",
            }
        ),
        "stock.company_profile": MappingProxyType(
            {
                "name": "stock_basic",
                "industry": "stock_basic",
                "board": "stock_basic",
                "main_business": "stock_company",
                "registered_address": "stock_company",
            }
        ),
        "stock.market_snapshot": MappingProxyType(
            {
                "price": "daily",
                "pct_change": "daily",
                "turnover": "daily",
                "market_cap": "daily_basic",
                "pe_ttm": "daily_basic",
                "pb": "daily_basic",
            }
        ),
        "stock.price_trend": MappingProxyType(
            {
                "latest_price": "daily",
                "return_20d": "daily",
                "return_60d": "daily",
                "ma20": "daily",
                "ma60": "daily",
                "volatility_20d": "daily",
                "max_drawdown": "daily",
            }
        ),
        "stock.financial_metrics": MappingProxyType(
            {
                "revenue": "income",
                "net_profit_parent": "income",
                "operating_cash_flow": "cashflow",
                "revenue_yoy": "fina_indicator",
                "net_profit_yoy": "fina_indicator",
                "operating_cash_flow_yoy": "fina_indicator",
                "gross_margin": "fina_indicator",
                "roe": "fina_indicator",
                "debt_to_asset": "fina_indicator",
            }
        ),
        "stock.announcement_search": MappingProxyType({"announcements": "anns_d"}),
    }
)


@dataclass(frozen=True)
class ApiRequirements:
    capability: str
    selection: FieldSelection
    api_fields: Mapping[str, tuple[str, ...]]
    missing_optional_fields: tuple[str, ...]
    missing_optional_apis: tuple[str, ...]

    @property
    def source_apis(self) -> list[str]:
        return list(self.api_fields)


def all_market_data_api_names() -> set[str]:
    return {
        api_name
        for field_apis in MARKET_DATA_FIELD_APIS.values()
        for api_name in field_apis.values()
    }


def default_field_selection(capability: str) -> FieldSelection:
    field_apis = MARKET_DATA_FIELD_APIS.get(capability)
    if field_apis is None:
        raise _invalid_capability_error(capability)
    return FieldSelection(required_fields=list(field_apis))


def resolve_api_requirements(
    capability: str,
    selection: FieldSelection,
    enabled_api_names: set[str],
) -> ApiRequirements:
    field_apis = MARKET_DATA_FIELD_APIS.get(capability)
    if field_apis is None:
        raise _invalid_capability_error(capability)

    unknown_fields = [
        field_name for field_name in selection.requested_fields if field_name not in field_apis
    ]
    if unknown_fields:
        raise MarketDataError(
            "invalid_field_request",
            "请求中包含当前市场数据能力不支持的字段。",
            details={"capability": capability, "unknown_fields": unknown_fields},
            retryable=False,
        )

    missing_required_fields = [
        field_name
        for field_name in selection.required_fields
        if field_apis[field_name] not in enabled_api_names
    ]
    if missing_required_fields:
        missing_required_apis = _unique(
            [field_apis[field_name] for field_name in missing_required_fields]
        )
        raise MarketDataError(
            "required_api_permission_missing",
            "当前数据源权限无法提供必需字段。",
            details={
                "capability": capability,
                "missing_fields": missing_required_fields,
                "required_apis": missing_required_apis,
            },
            retryable=False,
        )

    missing_optional_fields = [
        field_name
        for field_name in selection.optional_fields
        if field_apis[field_name] not in enabled_api_names
    ]
    missing_optional_apis = _unique(
        [field_apis[field_name] for field_name in missing_optional_fields]
    )
    api_fields: dict[str, list[str]] = {}
    for field_name in selection.requested_fields:
        api_name = field_apis[field_name]
        if api_name not in enabled_api_names:
            continue
        api_fields.setdefault(api_name, []).append(field_name)

    return ApiRequirements(
        capability=capability,
        selection=selection,
        api_fields=MappingProxyType(
            {api_name: tuple(fields) for api_name, fields in api_fields.items()}
        ),
        missing_optional_fields=tuple(missing_optional_fields),
        missing_optional_apis=tuple(missing_optional_apis),
    )


def build_tool_coverage(
    requirements: ApiRequirements,
    available_fields: set[str],
) -> ToolCoverage:
    selection = requirements.selection
    provided_fields = [
        field_name
        for field_name in selection.requested_fields
        if field_name in available_fields
    ]
    missing_required_fields = [
        field_name for field_name in selection.required_fields if field_name not in available_fields
    ]
    missing_optional_fields = [
        field_name for field_name in selection.optional_fields if field_name not in available_fields
    ]
    if missing_required_fields:
        status = "failed"
    elif missing_optional_fields:
        status = "partial"
    else:
        status = "complete"
    return ToolCoverage(
        status=status,
        requested_fields=selection.requested_fields,
        required_fields=selection.required_fields,
        optional_fields=selection.optional_fields,
        provided_fields=provided_fields,
        missing_required_fields=missing_required_fields,
        missing_optional_fields=missing_optional_fields,
        source_apis=requirements.source_apis,
    )


def optional_permission_warnings(requirements: ApiRequirements) -> list[str]:
    return [
        f"optional_api_permission_missing:{api_name}"
        for api_name in requirements.missing_optional_apis
    ]


def _invalid_capability_error(capability: str) -> MarketDataError:
    return MarketDataError(
        "invalid_field_request",
        "未知的市场数据能力。",
        details={"capability": capability},
        retryable=False,
    )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
