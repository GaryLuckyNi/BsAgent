import numpy as np

from app.schemas.tools import ToolSpec
from app.tools.search import ToolSearchIndex


class FakeEmbedder:
    def __init__(self, document_vectors, query_vector, *, fail_query=False):
        self.document_vectors = np.asarray(document_vectors, dtype=np.float32)
        self.query_vector = np.asarray(query_vector, dtype=np.float32)
        self.fail_query = fail_query
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts):
        self.document_calls += 1
        assert len(texts) == len(self.document_vectors)
        return self.document_vectors

    def embed_query(self, text):
        self.query_calls += 1
        if self.fail_query:
            raise RuntimeError("dense unavailable")
        return self.query_vector


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


def test_exact_tool_name_skips_query_embedding():
    embedder = FakeEmbedder(np.eye(3), [1, 0, 0])
    index = ToolSearchIndex.build(
        specs=_specs(), catalog_digest="digest-1", embedder=embedder
    )

    response = index.search(
        "请调用 mcp.tushare.daily 获取数据",
        top_k=2,
        candidate_k=3,
        allowed_tool_names={"mcp.tushare.daily"},
    )

    assert response.mode == "exact"
    assert [hit.tool_name for hit in response.hits] == ["mcp.tushare.daily"]
    assert response.hits[0].sources == ["exact"]
    assert embedder.query_calls == 0


def test_hybrid_search_fuses_bm25_and_dense_rankings():
    embedder = FakeEmbedder(
        [[1, 0], [0, 1], [0.2, 0.8]],
        [1, 0],
    )
    index = ToolSearchIndex.build(
        specs=_specs(), catalog_digest="digest-1", embedder=embedder
    )

    response = index.search("分析公司的利润与盈利能力", top_k=2, candidate_k=3)

    assert response.mode == "hybrid"
    assert response.hits[0].tool_name == "mcp.tushare.income"
    assert set(response.hits[0].sources) == {"bm25", "dense"}


def test_allowed_tools_are_filtered_before_candidate_limit():
    embedder = FakeEmbedder(
        [[1, 0], [0.9, 0.1], [0, 1]],
        [1, 0],
    )
    index = ToolSearchIndex.build(
        specs=_specs(), catalog_digest="digest-1", embedder=embedder
    )

    response = index.search(
        "公司数据",
        top_k=1,
        candidate_k=1,
        allowed_tool_names={"mcp.tushare.daily"},
    )

    assert [hit.tool_name for hit in response.hits] == ["mcp.tushare.daily"]


def test_dense_query_failure_falls_back_to_bm25():
    embedder = FakeEmbedder(np.eye(3), [1, 0, 0], fail_query=True)
    index = ToolSearchIndex.build(
        specs=_specs(), catalog_digest="digest-1", embedder=embedder
    )

    response = index.search("利润表", top_k=2, candidate_k=3)

    assert response.mode == "bm25_fallback"
    assert response.degraded is True
    assert response.hits[0].tool_name == "mcp.tushare.income"
    assert response.hits[0].sources == ["bm25"]


def test_equal_scores_keep_source_document_order():
    specs = [_spec("tool.a", "相同描述"), _spec("tool.b", "相同描述")]
    embedder = FakeEmbedder([[1, 0], [1, 0]], [1, 0])
    index = ToolSearchIndex.build(
        specs=specs, catalog_digest="digest-1", embedder=embedder
    )

    first = index.search("相同描述", top_k=2, candidate_k=2)
    second = index.search("相同描述", top_k=2, candidate_k=2)

    assert [hit.tool_name for hit in first.hits] == ["tool.a", "tool.b"]
    assert first == second


def test_empty_catalog_returns_empty_result_without_embedding_query():
    embedder = FakeEmbedder([], [1, 0])
    index = ToolSearchIndex.build(
        specs=[], catalog_digest="digest-empty", embedder=embedder
    )

    response = index.search("利润表", top_k=5, candidate_k=20)

    assert response.hits == []
    assert response.catalog_digest == "digest-empty"
    assert embedder.query_calls == 0
