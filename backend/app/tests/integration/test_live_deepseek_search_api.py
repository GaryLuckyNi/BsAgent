"""显式开启时验证独立搜索接口；默认不联网。"""

import asyncio
import os

import pytest
from app.core.config import settings
from app.search.deepseek_api import DeepSeekSearchApiClient

pytestmark = pytest.mark.skipif(
    os.getenv("BS_AGENT_RUN_LIVE_DEEPSEEK_TESTS") != "1",
    reason="需要显式开启 DeepSeek 真实接口测试。",
)


def test_live_deepseek_search_api_returns_sources_without_generated_answer():
    if not settings.deepseek_api_key:
        pytest.skip("未配置 DeepSeek API Key。")
    result = asyncio.run(
        DeepSeekSearchApiClient(
            api_key=settings.deepseek_api_key,
            base_url=settings.web_search_base_url,
            timeout_seconds=settings.web_search_timeout_seconds,
        ).search(query="长鑫存储 上市主体 股票代码 招股书", max_results=3)
    )
    assert result.results and result.answer == ""
    assert result.coverage.status == "unknown" and result.usage == {}
    assert any(row.snippet for row in result.results)
