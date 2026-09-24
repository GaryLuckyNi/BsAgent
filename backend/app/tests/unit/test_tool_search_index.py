import pytest

from app.schemas.tools import ToolSpec
from app.tools.search import ToolSearchIndex, ToolSearchUnavailableError


def _spec(name: str, description: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=description,
        input_schema={
            "type": "object",
            "properties": {"ts_code": {"type": "string", "description": "股票代码"}},
            "required": ["ts_code"],
        },
    )


def _specs():
    return [
        _spec("mcp.tushare.income", "查询公司利润表与盈利能力"),
        _spec("mcp.tushare.daily", "查询股票日线行情"),
        _spec("mcp.tushare.news", "查询上市公司新闻"),
    ]


def test_exact_tool_name_returns_exact_match():
    index = ToolSearchIndex.build(specs=_specs(), catalog_digest="digest-1")

    response = index.search(
        "请调用 mcp.tushare.daily 获取数据",
        top_k=2,
        candidate_k=3,
        allowed_tool_names={"mcp.tushare.daily"},
    )

    assert response.mode == "exact"
    assert [hit.tool_name for hit in response.hits] == ["mcp.tushare.daily"]
    assert response.hits[0].sources == ["exact"]


def test_bm25_search_returns_lexical_candidates_without_embeddings():
    index = ToolSearchIndex.build(specs=_specs(), catalog_digest="digest-1")

    response = index.search("分析公司的利润与盈利能力", top_k=2, candidate_k=3)

    assert response.mode == "bm25"
    assert response.hits[0].tool_name == "mcp.tushare.income"
    assert response.hits[0].sources == ["bm25"]
    assert response.hits[0].bm25_score is not None


def test_allowed_tools_are_filtered_before_candidate_limit():
    index = ToolSearchIndex.build(specs=_specs(), catalog_digest="digest-1")

    response = index.search(
        "公司利润表 股票日线行情",
        top_k=1,
        candidate_k=1,
        allowed_tool_names={"mcp.tushare.daily"},
    )

    assert [hit.tool_name for hit in response.hits] == ["mcp.tushare.daily"]


def test_bm25_search_omits_zero_score_candidates():
    index = ToolSearchIndex.build(specs=_specs(), catalog_digest="digest-1")

    response = index.search("unrelated_capability_xyz", top_k=2, candidate_k=3)

    assert response.mode == "bm25"
    assert response.hits == []


def test_bm25_failure_reports_tool_search_unavailable(monkeypatch):
    index = ToolSearchIndex.build(specs=_specs(), catalog_digest="digest-1")

    def fail_retrieve(*args, **kwargs):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(index._bm25, "retrieve", fail_retrieve)

    with pytest.raises(ToolSearchUnavailableError):
        index.search("利润表", top_k=2, candidate_k=3)


def test_equal_scores_keep_source_document_order():
    specs = [_spec("tool.a", "相同描述"), _spec("tool.b", "相同描述")]
    index = ToolSearchIndex.build(specs=specs, catalog_digest="digest-1")

    first = index.search("相同描述", top_k=2, candidate_k=2)
    second = index.search("相同描述", top_k=2, candidate_k=2)

    assert [hit.tool_name for hit in first.hits] == ["tool.a", "tool.b"]
    assert first == second


def test_empty_catalog_returns_empty_bm25_result():
    index = ToolSearchIndex.build(specs=[], catalog_digest="digest-empty")

    response = index.search("利润表", top_k=5, candidate_k=20)

    assert response.hits == []
    assert response.catalog_digest == "digest-empty"
    assert response.mode == "bm25"


def test_schema_aware_rerank_prefers_stock_announcement_over_index_announcement():
    specs = [
        ToolSpec(
            name="mcp.tushare.idx_anns",
            version="1.0.0",
            description="获取指数公告",
            input_schema={
                "type": "object",
                "properties": {
                    "index_code": {"type": "string", "description": "指数代码"}
                },
            },
        ),
        ToolSpec(
            name="mcp.tushare.stock_company",
            version="1.0.0",
            description="获取上市公司基本资料",
            input_schema={
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码"}
                },
            },
        ),
        ToolSpec(
            name="mcp.tushare.anns_d",
            version="1.0.0",
            description="获取上市公司公告和监管披露",
            input_schema={
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码"},
                    "ann_date": {"type": "string", "description": "公告日期"},
                },
            },
        ),
        ToolSpec(
            name="mcp.tushare.news",
            version="1.0.0",
            description="获取财经新闻",
            input_schema={
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "新闻来源"}
                },
            },
        ),
        ToolSpec(
            name="mcp.tushare.stk_holdertrade",
            version="1.0.0",
            description="获取上市公司重要股东近期及历史股份增减持数据",
            input_schema={
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码"},
                    "ann_date": {"type": "string", "description": "公告日期"},
                },
            },
        ),
        ToolSpec(
            name="mcp.tushare.stk_managers",
            version="1.0.0",
            description="获取上市公司管理层信息",
            input_schema={
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码"},
                    "ann_date": {"type": "string", "description": "公告日期"},
                },
            },
        ),
        ToolSpec(
            name="mcp.tushare.fina_audit",
            version="1.0.0",
            description="获取上市公司定期财务审计意见数据",
            input_schema={
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码"},
                    "ann_date": {"type": "string", "description": "公告日期"},
                },
            },
        ),
    ]
    index = ToolSearchIndex.build(
        specs=specs,
        catalog_digest="digest-schema-rerank",
    )

    response = index.search(
        "上市公司公告 监管披露 公告标题 PDF下载 财经新闻 新闻快讯 媒体报道",
        top_k=5,
        candidate_k=7,
    )

    assert response.hits[0].tool_name == "mcp.tushare.anns_d"
    assert "mcp.tushare.news" in [hit.tool_name for hit in response.hits[:3]]
    assert response.hits[0].lexical_score > response.hits[1].lexical_score
    assert response.hits[0].schema_score > 0
