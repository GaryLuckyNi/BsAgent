import asyncio
import os

import pytest

from app.llm.deepseek import DeepSeekClient


@pytest.mark.skipif(
    os.getenv("BS_AGENT_RUN_LIVE_DEEPSEEK_TESTS") != "1",
    reason="Set BS_AGENT_RUN_LIVE_DEEPSEEK_TESTS=1 to run live DeepSeek tests.",
)
def test_live_deepseek_llm_smoke_ping_pong():
    api_key = os.getenv("BS_AGENT_DEEPSEEK_API_KEY", "")
    if not api_key:
        pytest.skip("BS_AGENT_DEEPSEEK_API_KEY is required for live DeepSeek tests.")

    client = DeepSeekClient(
        api_key=api_key,
        model=os.getenv("BS_AGENT_DEEPSEEK_MODEL", "deepseek-v4-flash"),
        base_url=os.getenv("BS_AGENT_DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        timeout_seconds=float(os.getenv("BS_AGENT_LLM_TIMEOUT_SECONDS", "30")),
    )

    async def run_completion():
        return await client.complete(
            system_prompt="你是一个连通性测试助手。只输出 pong，不要输出其他内容。",
            user_prompt="ping",
            temperature=0.0,
            max_tokens=8,
        )

    result = asyncio.run(run_completion())

    assert "pong" in result.content.lower()
    assert result.provider == "deepseek"
