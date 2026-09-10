from types import SimpleNamespace

import pytest

from app.intent.catalog import BusinessIntentCatalog
from app.intent.schemas import BusinessIntentProfile


@pytest.fixture(scope="session")
def business_intent_catalog() -> BusinessIntentCatalog:
    return BusinessIntentCatalog(
        [
            BusinessIntentProfile(
                id="company_research.financial_report_analysis",
                intent_type="financial_research",
                domain="a_share_equity_research",
                subdomain="financial_report_analysis",
                trigger_keywords=[
                    "年报",
                    "季报",
                    "财报",
                    "利润",
                    "现金流",
                    "毛利率",
                    "ROE",
                ],
                trigger_examples=[
                    "分析宁德时代 2024 年报",
                    "看看万华化学 2025Q1 财报有什么变化",
                ],
            ),
            BusinessIntentProfile(
                id="company_research.risk_scan",
                intent_type="financial_research",
                domain="a_share_equity_research",
                subdomain="risk_scan",
                trigger_keywords=[
                    "风险",
                    "问询函",
                    "处罚",
                    "诉讼",
                    "减持",
                    "质押",
                    "隆基绿能",
                ],
                trigger_examples=[
                    "看看隆基绿能最近有什么风险",
                    "这家公司有没有监管或诉讼风险",
                ],
            ),
            BusinessIntentProfile(
                id="company_research.company_snapshot",
                intent_type="financial_research",
                domain="a_share_equity_research",
                subdomain="company_snapshot",
                trigger_keywords=[
                    "分析一下",
                    "基本面",
                    "公司速览",
                    "公司概览",
                    "主营业务",
                ],
                trigger_examples=[
                    "分析一下贵州茅台",
                    "帮我看一下 600519",
                    "宁德时代现在基本面怎么样",
                ],
            ),
        ]
    )


class _FakeBusinessIntentCatalogRuntime:
    refresh_seconds = 3600

    def __init__(self, catalog: BusinessIntentCatalog) -> None:
        self._snapshot = SimpleNamespace(catalog=catalog)

    def open(self):
        return self._snapshot

    def snapshot(self):
        return self._snapshot

    def refresh(self):
        return self._snapshot

    def check_health(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def fake_business_intent_catalog_runtime(
    business_intent_catalog: BusinessIntentCatalog,
) -> _FakeBusinessIntentCatalogRuntime:
    return _FakeBusinessIntentCatalogRuntime(business_intent_catalog)
