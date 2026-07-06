from app.market_data.provider import FixtureMarketDataProvider
from app.tools import calculate_sum, echo
from mcp.server.fastmcp import FastMCP


mcp = FastMCP(
    name="bs-agent-local-mcp",
    instructions="本地 MCP Server，用于 BsAgent 调用真实 MCP tools 的联调验证。",
)
market_data_provider = FixtureMarketDataProvider()


@mcp.tool(name="echo")
def echo_tool(text: str) -> dict:
    """返回输入文本，用于验证 MCP 调用链路。"""
    return echo(text)


@mcp.tool(name="calculate_sum")
def calculate_sum_tool(numbers: list[float]) -> dict:
    """计算一组数字的总和。"""
    return calculate_sum(numbers)


@mcp.tool(name="stock.resolve_company")
def stock_resolve_company_tool(query: str) -> dict:
    """将 A 股公司名、简称或代码标准化为证券代码。"""
    return market_data_provider.resolve_company(query).model_dump()


@mcp.tool(name="stock.company_profile")
def stock_company_profile_tool(symbol: str) -> dict:
    """查询 A 股公司基础资料。"""
    return market_data_provider.get_company_profile(symbol).model_dump()


@mcp.tool(name="stock.market_snapshot")
def stock_market_snapshot_tool(symbol: str) -> dict:
    """查询 A 股行情和估值快照。"""
    return market_data_provider.get_market_snapshot(symbol).model_dump()


@mcp.tool(name="stock.financial_metrics")
def stock_financial_metrics_tool(symbol: str, period: str) -> dict:
    """查询 A 股公司指定报告期的核心财务指标。"""
    return market_data_provider.get_financial_metrics(symbol, period).model_dump()


@mcp.tool(name="stock.announcement_search")
def stock_announcement_search_tool(symbol: str, date_range: str = "12m") -> dict:
    """查询 A 股公告元数据。"""
    return market_data_provider.search_announcements(symbol, date_range).model_dump()


@mcp.tool(name="stock.risk_events")
def stock_risk_events_tool(symbol: str, date_range: str = "6m") -> dict:
    """查询 A 股公司风险事件元数据。"""
    return market_data_provider.get_risk_events(symbol, date_range).model_dump()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
