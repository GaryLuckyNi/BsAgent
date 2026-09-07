import pytest
from pydantic import ValidationError

from app.tools.search.contracts import (
    ToolSearchContext,
    ToolSearchHit,
    ToolSearchInput,
    ToolSearchResponse,
)


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
        sources=["exact", "bm25", "dense"],
    )
    response = ToolSearchResponse(
        catalog_digest="catalog-v1",
        mode="hybrid",
        hits=[hit],
        degraded=False,
    )

    assert response.hits == [hit]
    assert response.hits[0].sources == ["exact", "bm25", "dense"]


def test_tool_search_response_has_empty_defaults():
    response = ToolSearchResponse(catalog_digest="catalog-v1", mode="exact")

    assert response.hits == []
    assert response.degraded is False


@pytest.mark.parametrize("mode", ["unknown", "dense"])
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
