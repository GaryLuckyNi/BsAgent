import json
import os
from pathlib import Path
from time import perf_counter

import numpy as np
import pytest

from app.schemas.tools import ToolSpec
from app.tools.search.embedding import (
    CachingToolTextEmbedder,
    FastEmbedToolTextEmbedder,
)
from app.tools.search.index import ToolSearchIndex


PROJECT_ROOT = Path(__file__).resolve().parents[4]
CASES_PATH = Path(__file__).with_name("tool_search_cases.json")
CATALOG_DIGEST = "tool-search-eval-v1"
RUN_EVAL = os.getenv("BS_AGENT_RUN_TOOL_SEARCH_EVAL", "").lower() in {
    "1",
    "true",
    "yes",
}

_TOOL_DESCRIPTIONS = {
    "mcp.tushare.stock_basic": "查询A股上市公司基础资料，包括证券代码、简称、公司全称、交易所、市场板块、上市状态、上市日期、地域和所属行业。",
    "mcp.tushare.income": "查询上市公司利润表，包括营业收入、营业成本、营业利润、利润总额、所得税、归母净利润和每股收益。",
    "mcp.tushare.balancesheet": "查询上市公司资产负债表，包括现金、应收账款、存货、固定资产、商誉、总资产、短期或长期负债及股东权益。",
    "mcp.tushare.cashflow": "查询上市公司现金流量表，包括经营活动、投资活动、筹资活动现金流和现金及现金等价物净增加额。",
    "mcp.tushare.fina_indicator": "查询财务分析指标，包括毛利率、净利率、ROE、ROA、资产负债率、流动比率、速动比率和周转率。",
    "mcp.tushare.daily": "查询A股日线行情和历史K线，包括开盘价、最高价、最低价、收盘价、涨跌幅、成交量和成交额。",
    "mcp.tushare.daily_basic": "查询股票每日估值指标，包括市盈率PE、市净率PB、市销率PS、股息率、换手率、总市值和流通市值。",
    "mcp.tushare.anns": "查询上市公司公告和监管披露，包括定期报告、临时公告、董事会决议、股东大会和风险提示。",
    "mcp.tushare.forecast": "查询上市公司业绩预告，包括预增、预减、扭亏、续亏、首亏、净利润区间、同比幅度和变动原因。",
    "mcp.tushare.express": "查询上市公司业绩快报，包括快速披露的营业收入、净利润、总资产、股东权益、每股收益和同比变化。",
    "mcp.tushare.news": "搜索财经新闻、公司新闻、证券资讯、舆情、媒体报道和并购融资等近期重大事件。",
    "mcp.tushare.research_report": "查询券商研报和研究报告，包括分析师、机构评级、目标价、盈利预测、每股收益一致预期和行业比较。",
}


def _load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name=name,
            version="1.0.0",
            description=description,
            input_schema={
                "type": "object",
                "properties": {
                    "ts_code": {
                        "type": "string",
                        "description": "Tushare 标准股票代码",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "查询起始日期",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "查询结束日期",
                    },
                },
                "required": ["ts_code"],
            },
            permission_scope=["mcp:tushare"],
        )
        for name, description in _TOOL_DESCRIPTIONS.items()
    ]


@pytest.fixture(scope="module")
def search_index() -> ToolSearchIndex:
    cache_dir = os.getenv(
        "BS_AGENT_TOOL_SEARCH_EMBEDDING_CACHE_DIR",
        str(PROJECT_ROOT / "data" / "models" / "fastembed"),
    )
    local_files_only = os.getenv(
        "BS_AGENT_TOOL_SEARCH_LOCAL_FILES_ONLY", "false"
    ).lower() in {"1", "true", "yes"}
    embedder = CachingToolTextEmbedder(
        FastEmbedToolTextEmbedder(
            model_name=os.getenv(
                "BS_AGENT_TOOL_SEARCH_EMBEDDING_MODEL",
                "BAAI/bge-small-zh-v1.5",
            ),
            cache_dir=cache_dir,
            local_files_only=local_files_only,
        ),
        catalog_digest=CATALOG_DIGEST,
        max_entries=256,
    )
    return ToolSearchIndex.build(
        specs=_tool_specs(),
        catalog_digest=CATALOG_DIGEST,
        embedder=embedder,
    )


def test_tool_search_eval_dataset_has_100_valid_chinese_cases():
    cases = _load_cases()

    assert len(cases) == 100
    assert all(case["query"].strip() for case in cases)
    assert all(case["permission_scopes"] == ["mcp:tushare"] for case in cases)
    assert all(case["relevant_tools"] for case in cases)
    assert {
        tool_name
        for case in cases
        for tool_name in case["relevant_tools"]
    }.issubset(_TOOL_DESCRIPTIONS)


@pytest.mark.tool_search_eval
@pytest.mark.skipif(
    not RUN_EVAL,
    reason="设置 BS_AGENT_RUN_TOOL_SEARCH_EVAL=true 后运行向量检索评测。",
)
def test_tool_search_recall_at_5_is_at_least_95_percent(search_index):
    cases = _load_cases()
    matched = 0
    for case in cases:
        response = search_index.search(case["query"], top_k=5, candidate_k=12)
        returned = {hit.tool_name for hit in response.hits}
        matched += bool(returned.intersection(case["relevant_tools"]))

    recall_at_5 = matched / len(cases)
    assert recall_at_5 >= 0.95, f"Recall@5={recall_at_5:.2%}，低于 95% 门槛。"


@pytest.mark.tool_search_eval
@pytest.mark.skipif(
    not RUN_EVAL,
    reason="设置 BS_AGENT_RUN_TOOL_SEARCH_EVAL=true 后运行向量检索评测。",
)
def test_warm_tool_search_p95_is_at_most_150_ms(search_index):
    cases = _load_cases()
    for case in cases:
        search_index.search(case["query"], top_k=5, candidate_k=12)

    elapsed_ms = []
    for _ in range(3):
        for case in cases:
            started = perf_counter()
            search_index.search(case["query"], top_k=5, candidate_k=12)
            elapsed_ms.append((perf_counter() - started) * 1000)

    p95_ms = float(np.percentile(elapsed_ms, 95))
    assert p95_ms <= 150, f"工具检索 warm p95={p95_ms:.2f}ms，超过 150ms 门槛。"
