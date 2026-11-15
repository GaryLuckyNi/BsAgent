import pytest
from app.schemas.tools import ToolSpec
from app.tools.candidate_resolver import ToolCandidateResolver
from app.tools.search import ToolSearchHit, ToolSearchResponse


def _spec(name: str, scopes: list[str] | None = None) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=name,
        input_schema={"type": "object", "properties": {}},
        permission_scope=scopes or [],
    )


class FakeSearchService:
    catalog_digest = "digest"

    def __init__(self, hits):
        self.hits = hits
        self.last_request = None
        self.specs = {
            hit.tool_name: _spec(hit.tool_name, ["mcp:tushare"])
            for hit in hits
        }
        self.specs["web_search"] = _spec("web_search", ["web:search"])

    def search(self, request, *, context):
        self.last_request = request
        return ToolSearchResponse(
            catalog_digest="digest", mode="bm25", hits=self.hits
        )

    def resolve_specs(self, names):
        return [self.specs[name] for name in names]

    def execution_binding_digest(self, name, version):
        return "binding-1"


def test_resolver_only_excludes_observations_that_can_be_matched_before_arguments_exist():
    from app.tools.runtime_capabilities import CapabilityObservation, CapabilityScope

    service = FakeSearchService([_hit("mcp.tushare.daily")])
    resolver = ToolCandidateResolver(search_service=service)
    scope = CapabilityScope("t", "u", "r", "mcp.tushare.daily", "1.0.0", "binding-1", None)
    kwargs = {
        "task_goal": "查询日线", "tenant_id": "t", "user_id": "u", "request_id": "r",
        "permission_scopes": ("mcp:tushare", "web:search"), "has_successful_dependencies": False,
    }
    argument_only = CapabilityObservation("old", scope, "arguments", "fp", None)
    result = resolver.resolve(**kwargs, capability_observations=(argument_only,))
    assert "mcp.tushare.daily" in [item.name for item in result.candidates]
    tool_wide = CapabilityObservation("old", scope, "tool", "fp", None)
    result = resolver.resolve(**kwargs, capability_observations=(tool_wide,))
    assert [item.name for item in result.candidates] == ["web_search"]
    assert result.availability_skips[0]["observation_call_id"] == "old"
    assert service.catalog_digest == "digest"


def _hit(
    name: str,
    *,
    rank=1,
    sources=("bm25",),
    lexical_score=1.0,
):
    return ToolSearchHit(
        tool_name=name,
        tool_version="1.0.0",
        description=name,
        rank=rank,
        sources=list(sources),
        lexical_score=lexical_score,
    )


def test_resolver_excludes_search_and_web_platform_tools():
    resolver = ToolCandidateResolver(
        search_service=FakeSearchService([
            _hit("mcp.tushare.daily"),
            _hit("web_search"),
            _hit("search_tool"),
            _hit("mcp.tushare.daily_basic"),
        ])
    )
    result = resolver.resolve(
        task_goal="获取银之杰近期日线价格和成交量",
        tenant_id="tenant",
        user_id="user",
        permission_scopes=("mcp:tushare", "web:search"),
        has_successful_dependencies=False,
    )
    assert [item.name for item in result.candidates] == [
        "mcp.tushare.daily", "mcp.tushare.daily_basic", "web_search"
    ]
    assert result.tool_choice == "auto"
    assert result.fallback_reason is None


def test_resolver_uses_planner_tool_search_query_instead_of_entity_name():
    service = FakeSearchService([_hit("mcp.tushare.anns_d")])
    resolver = ToolCandidateResolver(search_service=service)

    resolver.resolve(
        task_goal="查询银之杰（300085.SZ）的近期资讯",
        tool_search_query="上市公司公告 监管披露 公告标题 PDF下载 财经新闻 新闻快讯 媒体报道",
        tenant_id="tenant",
        user_id="user",
        permission_scopes=("mcp:tushare", "web:search"),
        has_successful_dependencies=False,
    )

    assert service.last_request.query == (
        "上市公司公告 监管披露 公告标题 PDF下载 财经新闻 新闻快讯 媒体报道"
    )


def test_resolver_requires_web_without_candidates_or_dependencies():
    result = ToolCandidateResolver(search_service=FakeSearchService([])).resolve(
        task_goal="获取最新外部事实",
        tenant_id="tenant",
        user_id="user",
        permission_scopes=("web:search",),
        has_successful_dependencies=False,
    )
    assert [item.name for item in result.candidates] == ["web_search"]
    assert result.tool_choice == "auto"
    assert result.fallback_reason == "no_specialized_candidate"


def test_resolver_allows_optional_web_with_dependencies():
    result = ToolCandidateResolver(search_service=FakeSearchService([])).resolve(
        task_goal="综合已有材料，必要时补充最新事件",
        tenant_id="tenant",
        user_id="user",
        permission_scopes=("web:search",),
        has_successful_dependencies=True,
    )
    assert result.tool_choice == "auto"


def test_resolver_rejects_weak_bm25_only_overlap():
    result = ToolCandidateResolver(
        search_service=FakeSearchService([
            _hit(
                "mcp.tushare.stock_company",
                sources=("bm25",),
                lexical_score=0.05,
            )
        ])
    ).resolve(
        task_goal="查询银之杰近期资讯",
        tenant_id="tenant",
        user_id="user",
        permission_scopes=("mcp:tushare", "web:search"),
        has_successful_dependencies=False,
    )

    assert [item.name for item in result.candidates] == ["web_search"]
    assert result.tool_choice == "auto"


def test_resolver_keeps_specialized_candidates_even_when_web_ranks_first():
    result = ToolCandidateResolver(
        search_service=FakeSearchService([
            _hit("web_search", rank=1),
            _hit("rag_search", rank=2),
        ])
    ).resolve(
        task_goal="获取今日全球股市、汇率、商品和国债收益率",
        tenant_id="tenant",
        user_id="user",
        permission_scopes=("mcp:tushare", "web:search"),
        has_successful_dependencies=False,
    )

    assert [item.name for item in result.candidates] == ["rag_search", "web_search"]
    assert result.fallback_reason is None


def test_web_exposure_respects_skill_scope_even_outside_search_hits():
    result = ToolCandidateResolver(search_service=FakeSearchService([])).resolve(
        task_goal="搜索", tenant_id="t", user_id="u", permission_scopes=("web:search",),
        skill_allowed_tools=("mcp.*",), has_successful_dependencies=False,
    )
    assert result.candidates == ()


def test_specialized_search_excludes_platform_tools_before_top_k():
    class ScopedSearch(FakeSearchService):
        def search(self, request, *, context):
            assert set(context.excluded_tools) == {"web_search", "search_tool"}
            return super().search(request, context=context)
    service = ScopedSearch([_hit("mcp.tushare.daily")])
    result = ToolCandidateResolver(search_service=service, top_k=1).resolve(
        task_goal="行情", tenant_id="t", user_id="u", permission_scopes=("mcp:tushare", "web:search"), has_successful_dependencies=False)
    assert [spec.name for spec in result.candidates] == ["mcp.tushare.daily", "web_search"]


def _planning_resolution(*, scopes=("mcp:tushare",), description=None, **changes):
    from app.tools.planning_candidates import PlanningToolSnapshot, project_card

    service = FakeSearchService([_hit("mcp.tushare.stock_basic")])
    if description:
        service.specs["mcp.tushare.stock_basic"] = service.specs["mcp.tushare.stock_basic"].model_copy(
            update={"description": description})
    card = project_card(service.specs["mcp.tushare.stock_basic"])
    snapshot = PlanningToolSnapshot(request_id="r", catalog_digest="digest", query_fingerprint="q", candidates=[card])
    kwargs = {"task_goal": "取得基础信息", "tenant_id": "t", "user_id": "u", "request_id": "r",
              "permission_scopes": scopes, "has_successful_dependencies": False, "web_search_allowed": False,
              "planning_snapshot": snapshot, "candidate_refs": (card.ref,)}
    kwargs.update(changes)
    return service, ToolCandidateResolver(search_service=service), kwargs


def test_planning_candidates_are_reused_without_new_search():
    service, resolver, kwargs = _planning_resolution()
    result = resolver.resolve(**kwargs)
    assert service.last_request is None
    assert result.source == "planning_reuse"
    assert [spec.name for spec in result.candidates] == ["mcp.tushare.stock_basic"]


def test_planning_reuse_rechecks_declared_permissions_without_searching():
    service, resolver, kwargs = _planning_resolution(scopes=())
    result = resolver.resolve(**kwargs)
    assert not result.candidates and service.last_request is None


@pytest.mark.parametrize("changes", [
    {"request_id": "other"}, {"candidate_refs": ("tool:unknown@1",)},
    {"planning_snapshot": None},
])
def test_planning_reuse_rejects_unbound_references(changes):
    _service, resolver, kwargs = _planning_resolution(**changes)
    with pytest.raises(ValueError, match="候选"):
        resolver.resolve(**kwargs)


def test_planning_reuse_rejects_changed_catalog_or_version():
    from app.tools.registry import ToolCatalogChangedError

    service, resolver, kwargs = _planning_resolution()
    service.specs["mcp.tushare.stock_basic"] = service.specs["mcp.tushare.stock_basic"].model_copy(update={"version": "2"})
    with pytest.raises(ToolCatalogChangedError):
        resolver.resolve(**kwargs)
    service.catalog_digest = "changed"
    with pytest.raises(ToolCatalogChangedError):
        resolver.resolve(**kwargs)


def test_incompatible_planning_candidates_allow_one_task_search():
    service, resolver, kwargs = _planning_resolution(description="美股公司基础信息", task_goal="查询A股公司基础信息")
    result = resolver.resolve(**kwargs)
    assert service.last_request is not None
    assert result.source == "task_search"
    assert not result.candidates


def test_planning_reuse_respects_runtime_denial_without_rediscovery():
    from app.tools.runtime_capabilities import CapabilityObservation, CapabilityScope

    service, resolver, kwargs = _planning_resolution(web_search_allowed=True, scopes=("mcp:tushare", "web:search"))
    scope = CapabilityScope("t", "u", "r", "mcp.tushare.stock_basic", "1.0.0", "binding-1", None)
    result = resolver.resolve(**kwargs, capability_observations=(CapabilityObservation("old", scope, "tool", "fp", None),))
    assert service.last_request is None
    assert [spec.name for spec in result.candidates] == ["web_search"]
    assert result.availability_skips[0]["observation_call_id"] == "old"


def test_planning_reuse_preserves_retrieval_provenance_for_observability():
    from app.graph.nodes.tool_agent import _selection_confidence

    _service, resolver, kwargs = _planning_resolution()
    snapshot = kwargs["planning_snapshot"]
    kwargs["planning_snapshot"] = snapshot.model_copy(update={"candidates": [
        snapshot.candidates[0].model_copy(update={"sources": ["bm25"]})
    ]})
    result = resolver.resolve(**kwargs)
    assert _selection_confidence("mcp.tushare.stock_basic", result) == 0.75
