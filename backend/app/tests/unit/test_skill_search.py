from app.skills.definitions import SkillCard
from app.skills.search import SkillSearchService


def _card(
    name: str,
    summary: str,
    *,
    domains: list[str] | None = None,
    subdomains: list[str] | None = None,
) -> SkillCard:
    return SkillCard(
        name=name,
        summary=summary,
        domains=domains or ["a_share_equity_research"],
        subdomains=subdomains or ["company_snapshot"],
        definition_hash=f"hash:{name}",
    )


def test_skill_search_filters_domain_and_subdomain_before_ranking():
    service = SkillSearchService(
        [
            _card("company_snapshot", "分析上市公司基本面和市场表现"),
            _card(
                "risk_scan",
                "扫描公司公告和监管风险",
                subdomains=["risk_scan"],
            ),
            _card(
                "bond_research",
                "分析信用债风险",
                domains=["fixed_income_research"],
            ),
        ]
    )

    hits = service.search(
        query="分析公司盈利能力和基本面",
        domain="a_share_equity_research",
        subdomain="company_snapshot",
        top_k=3,
    )

    assert [card.name for card in hits] == ["company_snapshot"]


def test_skill_search_returns_top_k_in_relevance_order():
    service = SkillSearchService(
        [
            _card("company_snapshot", "分析公司基本面、业务和行情"),
            _card("valuation_review", "分析公司估值和市盈率"),
            _card("generic_research", "通用公司研究"),
        ]
    )

    hits = service.search(
        query="评估公司估值市盈率",
        domain="a_share_equity_research",
        subdomain="company_snapshot",
        top_k=2,
    )

    assert hits[0].name == "valuation_review"
    assert len(hits) == 2


def test_skill_search_has_no_tenant_or_version_selector():
    parameters = SkillSearchService.search.__annotations__

    assert "tenant_id" not in parameters
    assert "version" not in parameters
