import os

import pytest

from app.market_data.config import MarketDataProviderSettings, build_market_data_provider
from app.market_data.schemas import FieldSelection


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_TUSHARE_SMOKE") != "1" or not os.getenv("TUSHARE_TOKEN"),
    reason="未显式开启真实 Tushare smoke test，或未注入 TUSHARE_TOKEN。",
)


def test_live_tushare_stock_basic_smoke():
    """只验证管理员已声明的 stock_basic，不探测 Token 的其他权限。"""

    provider = build_market_data_provider(
        MarketDataProviderSettings(
            status="configured",
            provider="tushare",
            token=os.environ["TUSHARE_TOKEN"],
            enabled_api_names={"stock_basic"},
            stock_basic_cache_ttl_seconds=0,
        )
    )

    result = provider.resolve_company(
        os.getenv("TUSHARE_SMOKE_SYMBOL", "600519.SH"),
        FieldSelection(required_fields=["symbol", "name"]),
    )

    assert result.status == "complete"
    assert result.symbol
    assert result.name
    assert result.coverage.source_apis == ["stock_basic"]
