from collections.abc import Sequence
from inspect import signature
import os
from typing import Annotated, Literal

from app.market_data.capabilities import MARKET_DATA_FIELD_APIS
from app.market_data.config import build_configured_market_data_provider
from app.market_data.provider import MarketDataError
from app.market_data.schemas import FieldSelection, MarketDataResult
from app.tools import echo
from mcp.server.fastmcp import FastMCP
from pydantic import Field, ValidationError


mcp = FastMCP(
    name="bs-agent-local-mcp",
    instructions="本地 MCP Server，用于 BsAgent 调用真实 MCP tools 的联调验证。",
    host=os.getenv("MCP_SERVER_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_SERVER_PORT", "8011")),
    streamable_http_path=os.getenv("MCP_SERVER_PATH", "/mcp"),
    stateless_http=True,
    json_response=True,
)
market_data_provider = build_configured_market_data_provider()

ResolveCompanyField = Literal[*tuple(MARKET_DATA_FIELD_APIS["stock.resolve_company"])]
CompanyProfileField = Literal[*tuple(MARKET_DATA_FIELD_APIS["stock.company_profile"])]
MarketSnapshotField = Literal[*tuple(MARKET_DATA_FIELD_APIS["stock.market_snapshot"])]
PriceTrendField = Literal[*tuple(MARKET_DATA_FIELD_APIS["stock.price_trend"])]
FinancialMetricsField = Literal[*tuple(MARKET_DATA_FIELD_APIS["stock.financial_metrics"])]
AnnouncementSearchField = Literal[
    *tuple(MARKET_DATA_FIELD_APIS["stock.announcement_search"])
]
AnnouncementRequiredFields = Annotated[
    list[AnnouncementSearchField],
    Field(
        json_schema_extra={
            "items": {
                "type": "string",
                "enum": list(MARKET_DATA_FIELD_APIS["stock.announcement_search"]),
            }
        }
    ),
]
AnnouncementOptionalFields = Annotated[
    tuple[AnnouncementSearchField, ...],
    Field(
        json_schema_extra={
            "items": {
                "type": "string",
                "enum": list(MARKET_DATA_FIELD_APIS["stock.announcement_search"]),
            }
        }
    ),
]


@mcp.tool(name="echo")
def echo_tool(text: str) -> dict:
    """返回输入文本，用于验证 MCP 调用链路。"""
    return echo(text)


@mcp.tool(name="stock.resolve_company")
def stock_resolve_company_tool(
    query: str,
    required_fields: list[ResolveCompanyField],
    optional_fields: tuple[ResolveCompanyField, ...] = (),
) -> dict:
    """将 A 股公司名、简称或代码标准化为证券代码。"""
    return invoke_market_data(
        "resolve_company",
        query,
        required_fields=required_fields,
        optional_fields=optional_fields,
    )


@mcp.tool(name="stock.company_profile")
def stock_company_profile_tool(
    symbol: str,
    required_fields: list[CompanyProfileField],
    optional_fields: tuple[CompanyProfileField, ...] = (),
) -> dict:
    """查询 A 股公司基础资料。"""
    return invoke_market_data(
        "get_company_profile",
        symbol,
        required_fields=required_fields,
        optional_fields=optional_fields,
    )


@mcp.tool(name="stock.market_snapshot")
def stock_market_snapshot_tool(
    symbol: str,
    required_fields: list[MarketSnapshotField],
    optional_fields: tuple[MarketSnapshotField, ...] = (),
) -> dict:
    """查询 A 股行情和估值快照。"""
    return invoke_market_data(
        "get_market_snapshot",
        symbol,
        required_fields=required_fields,
        optional_fields=optional_fields,
    )


@mcp.tool(name="stock.price_trend")
def stock_price_trend_tool(
    symbol: str,
    required_fields: list[PriceTrendField],
    window_days: Annotated[int, Field(ge=20, le=250)] = 120,
    optional_fields: tuple[PriceTrendField, ...] = (),
) -> dict:
    """计算 A 股中短期收益、均线、波动率与最大回撤。"""
    return invoke_market_data(
        "get_price_trend",
        symbol,
        window_days,
        required_fields=required_fields,
        optional_fields=optional_fields,
    )


@mcp.tool(name="stock.financial_metrics")
def stock_financial_metrics_tool(
    symbol: str,
    period: str,
    required_fields: list[FinancialMetricsField],
    optional_fields: tuple[FinancialMetricsField, ...] = (),
) -> dict:
    """查询 A 股公司指定报告期的核心财务指标。"""
    return invoke_market_data(
        "get_financial_metrics",
        symbol,
        period,
        required_fields=required_fields,
        optional_fields=optional_fields,
    )


@mcp.tool(name="stock.announcement_search")
def stock_announcement_search_tool(
    symbol: str,
    required_fields: AnnouncementRequiredFields,
    date_range: str = "12m",
    optional_fields: AnnouncementOptionalFields = (),
) -> dict:
    """查询 A 股公告元数据。"""
    return invoke_market_data(
        "search_announcements",
        symbol,
        date_range,
        required_fields=required_fields,
        optional_fields=optional_fields,
    )


def invoke_market_data(
    method_name: str,
    *args,
    required_fields: Sequence[str],
    optional_fields: Sequence[str] | None,
) -> dict:
    """将 Provider 结果收敛为 MCP 稳定业务信封。"""

    try:
        selection = FieldSelection(
            required_fields=list(required_fields),
            optional_fields=list(optional_fields or ()),
        )
    except ValidationError:
        return {
            "status": "failed",
            "error": {
                "code": "invalid_field_request",
                "message": "字段选择参数无效。",
                "details": {"reason": "field_selection_invalid"},
                "retryable": False,
            },
        }

    try:
        method = getattr(market_data_provider, method_name)
        if "selection" in signature(method).parameters:
            result = method(*args, selection=selection)
        else:
            result = method(*args)
    except MarketDataError as exc:
        return {
            "status": "failed",
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "retryable": exc.retryable,
            },
        }

    return _success_envelope(result)


def _success_envelope(result: MarketDataResult) -> dict:
    payload = result.model_dump(mode="json")
    status = payload.pop("status")
    coverage = payload.pop("coverage")
    return {"status": status, "data": payload, "coverage": coverage}


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
