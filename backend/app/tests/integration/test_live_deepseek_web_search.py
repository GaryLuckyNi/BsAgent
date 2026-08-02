import asyncio
import os

import pytest

from app.core.config import settings
from app.search.deepseek import DeepSeekWebSearchClient


pytestmark = pytest.mark.skipif(
    os.getenv("BS_AGENT_RUN_LIVE_DEEPSEEK_TESTS") != "1",
    reason="需要显式开启 DeepSeek live tests。",
)


def test_live_deepseek_web_search_returns_verifiable_sources():
    api_key = os.getenv("BS_AGENT_DEEPSEEK_API_KEY", "")
    if not api_key:
        pytest.skip("BS_AGENT_DEEPSEEK_API_KEY is required for live DeepSeek tests.")
    result = asyncio.run(
        DeepSeekWebSearchClient(
            api_key=api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.web_search_timeout_seconds,
        ).search(
            query="长鑫存储 上市主体 股票代码 招股书",
            max_results=3,
        )
    )

    assert result.results
    assert all(str(item.url).startswith(("http://", "https://")) for item in result.results)
