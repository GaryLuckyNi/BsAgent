from collections.abc import Callable
from functools import wraps
from typing import Any

import pytest

from app.intent.catalog import BusinessIntentCatalog
from app.intent.schemas import BusinessIntentProfile


_GRAPH_RUNTIME_ENTRYPOINTS = (
    "build_question_answer_graph",
    "run_question_answer_graph",
    "stream_question_answer_graph",
    "resume_question_answer_graph",
)


@pytest.fixture(scope="session")
def business_intent_catalog() -> BusinessIntentCatalog:
    return BusinessIntentCatalog(
        [
            BusinessIntentProfile(
                id="company_research.financial_report_analysis",
                intent="task_workflow",
                domain="a_share_equity_research",
                sub_intent="financial_report_analysis",
                candidate_skill="financial_report_analysis",
                candidate_tool=None,
                risk_level="medium",
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
                required_capabilities=[
                    "mcp_tool",
                    "rag_retrieval",
                    "financial_verifier",
                ],
            ),
            BusinessIntentProfile(
                id="company_research.risk_scan",
                intent="task_workflow",
                domain="a_share_equity_research",
                sub_intent="risk_scan",
                candidate_skill="risk_scan",
                candidate_tool=None,
                risk_level="medium",
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
                required_capabilities=[
                    "mcp_tool",
                    "rag_retrieval",
                    "financial_verifier",
                ],
            ),
            BusinessIntentProfile(
                id="company_research.company_snapshot",
                intent="task_workflow",
                domain="a_share_equity_research",
                sub_intent="company_snapshot",
                candidate_skill="company_snapshot",
                candidate_tool=None,
                risk_level="medium",
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
                required_capabilities=[
                    "mcp_tool",
                    "rag_retrieval",
                    "financial_verifier",
                ],
            ),
        ]
    )


def _with_default_business_intent_catalog(
    entrypoint: Callable[..., Any],
    business_intent_catalog: BusinessIntentCatalog,
) -> Callable[..., Any]:
    @wraps(entrypoint)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("business_intent_catalog", business_intent_catalog)
        return entrypoint(*args, **kwargs)

    return wrapped


@pytest.fixture(autouse=True)
def bind_test_business_intent_catalog(
    request,
    monkeypatch,
    business_intent_catalog: BusinessIntentCatalog,
) -> None:
    for name in _GRAPH_RUNTIME_ENTRYPOINTS:
        entrypoint = getattr(request.module, name, None)
        if entrypoint is None or getattr(entrypoint, "__module__", None) != ("app.graph.runtime"):
            continue
        monkeypatch.setattr(
            request.module,
            name,
            _with_default_business_intent_catalog(
                entrypoint,
                business_intent_catalog,
            ),
        )
