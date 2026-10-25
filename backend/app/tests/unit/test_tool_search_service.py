import pytest
from app.schemas.tools import ToolCallResult
from app.tools.base import Tool
from app.tools.registry import ToolRegistry
from app.tools.search.contracts import (
    ToolSearchContext,
    ToolSearchHit,
    ToolSearchInput,
    ToolSearchResponse,
)
from app.tools.search.index import ToolSearchIndex
from app.tools.search.service import ToolSearchService
from pydantic import ValidationError


class _Input(ToolSearchInput):
    pass


class _CatalogTool(Tool[_Input]):
    input_model = _Input
    version = "1.0.0"

    def __init__(self, *, name: str, description: str, scopes=()):
        self.name = name
        self.description = description
        self.permission_scope = list(scopes)

    async def run(self, input_data):
        return ToolCallResult(
            tool_call_id="",
            tool_name=self.name,
            status="success",
            result={},
            trace_id="",
        )


def _search_service():
    registry = ToolRegistry()
    registry.register(
        _CatalogTool(
            name="mcp.tushare.income",
            description="查询公司利润表",
            scopes=("market:financial",),
        )
    )
    registry.register(
        _CatalogTool(
            name="mcp.tushare.daily",
            description="查询股票日线行情",
            scopes=("market:quote",),
        )
    )
    snapshot = registry.snapshot()
    index = ToolSearchIndex.build(
        specs=snapshot.specs(),
        catalog_digest=snapshot.catalog_digest,
    )
    return ToolSearchService(registry=snapshot, index=index, candidate_k=20)


@pytest.mark.parametrize("incompatible_description",[
    "/数据接口/港股数据/港股日线行情-查询日线收盘价",
    "/数据接口/美股数据/美股财务指标-查询财务指标",
    "/数据接口/指数专题/国际主要指数-查询历史日线",
    "/数据接口/指数专题/指数实时日线-查询实时行情",
])
@pytest.mark.parametrize("negative_suffix",["","，不得使用美股数据，不查询海外指数"])
def test_explicit_market_and_history_constraints_apply_before_top_k(incompatible_description,negative_suffix):
    registry=ToolRegistry()
    registry.register(_CatalogTool(name="mcp.provider.arbitrary_wrong",description=incompatible_description))
    registry.register(_CatalogTool(name="mcp.provider.arbitrary_right",description="A股指数历史日线收盘价查询"))
    snapshot=registry.snapshot()
    service=ToolSearchService(registry=snapshot,index=ToolSearchIndex.build(
        specs=snapshot.specs(),catalog_digest=snapshot.catalog_digest),candidate_k=20)
    result=service.search(ToolSearchInput(query="mcp.provider.arbitrary_wrong 查询日线行情",top_k=1),context=ToolSearchContext(
        tenant_id="t",user_id="u",task_goal="查询A股上证指数000001.SH在2025年9月1日的历史收盘价"+negative_suffix))
    assert [h.tool_name for h in result.hits]==["mcp.provider.arbitrary_right"]


def test_tool_search_input_normalizes_whitespace():
    search_input = ToolSearchInput(query="  查找\n  公司\t公告  ")

    assert search_input.query == "查找 公司 公告"
    assert search_input.top_k == 5


def test_tool_search_input_forbids_extra_fields():
    with pytest.raises(ValidationError):
        ToolSearchInput(query="查询利润表", unsupported=True)


@pytest.mark.parametrize("query", ["", " \n\t ", "x" * 513])
def test_tool_search_input_enforces_query_length(query):
    with pytest.raises(ValidationError):
        ToolSearchInput(query=query)


@pytest.mark.parametrize("top_k", [0, 9])
def test_tool_search_input_enforces_top_k_bounds(top_k):
    with pytest.raises(ValidationError):
        ToolSearchInput(query="财报检索", top_k=top_k)


def test_tool_search_response_preserves_ranked_contract():
    hit = ToolSearchHit(
        tool_name="rag_search",
        tool_version="1.0.0",
        description="检索企业知识库证据片段",
        required_arguments=["query"],
        rank=1,
        sources=["bm25"],
    )
    response = ToolSearchResponse(
        catalog_digest="catalog-v1",
        mode="bm25",
        hits=[hit],
    )

    assert response.hits == [hit]
    assert response.hits[0].sources == ["bm25"]
    assert "dense_score" not in response.model_dump_json()
    assert "fusion_score" not in response.model_dump_json()
    assert "degraded" not in response.model_dump_json()


def test_tool_search_response_has_empty_defaults():
    response = ToolSearchResponse(catalog_digest="catalog-v1", mode="exact")

    assert response.hits == []


@pytest.mark.parametrize("mode", ["unknown", "dense", "hybrid", "bm25_fallback"])
def test_tool_search_response_rejects_unknown_mode(mode):
    with pytest.raises(ValidationError):
        ToolSearchResponse(
            catalog_digest="catalog-v1",
            mode=mode,
        )


def test_tool_search_hit_rejects_unknown_source():
    with pytest.raises(ValidationError):
        ToolSearchHit(
            tool_name="rag_search",
            tool_version="1.0.0",
            description="检索企业知识库证据片段",
            required_arguments=["query"],
            rank=1,
            sources=["keyword"],
        )


def test_tool_search_context_is_frozen_and_forbids_extra_fields():
    context = ToolSearchContext(
        tenant_id="tenant-1",
        user_id="user-1",
        permission_scopes=("rag:retrieve",),
        skill_allowed_tools=("rag_search",),
    )

    with pytest.raises(ValidationError):
        context.tenant_id = "tenant-2"

    with pytest.raises(ValidationError):
        ToolSearchContext(
            tenant_id="tenant-1",
            user_id="user-1",
            permission_scopes=(),
            skill_allowed_tools=None,
            session_id="session-1",
        )


def test_tool_search_context_has_empty_optional_defaults():
    context = ToolSearchContext(tenant_id="tenant-1", user_id="user-1")

    assert context.permission_scopes == ()
    assert context.skill_allowed_tools is None


def test_search_service_excludes_tools_without_required_scope():
    response = _search_service().search(
        ToolSearchInput(query="mcp.tushare.income", top_k=5),
        context=ToolSearchContext(
            tenant_id="tenant-1",
            user_id="user-1",
            permission_scopes=("market:quote",),
        ),
    )

    assert {hit.tool_name for hit in response.hits} == {"mcp.tushare.daily"}


def test_search_service_applies_skill_patterns_before_returning_hits():
    response = _search_service().search(
        ToolSearchInput(query="mcp.tushare.income", top_k=5),
        context=ToolSearchContext(
            tenant_id="tenant-1",
            user_id="user-1",
            permission_scopes=("*",),
            skill_allowed_tools=("mcp.tushare.daily",),
        ),
    )

    assert {hit.tool_name for hit in response.hits} == {"mcp.tushare.daily"}


def test_search_service_resolves_only_specs_from_same_catalog():
    service = _search_service()

    specs = service.resolve_specs(["mcp.tushare.income"])

    assert [spec.name for spec in specs] == ["mcp.tushare.income"]


def test_search_service_rejects_index_from_different_catalog():
    registry = ToolRegistry().snapshot()
    index = ToolSearchIndex.build(specs=[], catalog_digest="different")

    with pytest.raises(ValueError, match="digest"):
        ToolSearchService(registry=registry, index=index, candidate_k=20)
