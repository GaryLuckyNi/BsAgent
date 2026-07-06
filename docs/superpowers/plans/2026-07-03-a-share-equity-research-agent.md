# A Share Equity Research Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first working A 股公司研究 Agent vertical slice for 投研分析师: company snapshot, financial report analysis, and risk scan.

**Architecture:** Keep the existing LangGraph Agent chain and add finance capability through business intent config, admin-backed Skill Registry, MCP-discovered stock tools, structured analysis, and a Financial Verifier wrapper. RAG remains retrieval-only; market data is accessed only through Tool Agent and MCP tools. Skill definitions come from the admin registry when configured; local code supplies a generic configured-skill executor and a local fallback for tests.

**Tech Stack:** Python, FastAPI, LangGraph, MCP stdio server, Pydantic, pytest, ruff, existing BsAgent RAG tool and tracing harness.

---

## File Structure

- Create `mcp server/app/market_data/schemas.py`: Pydantic contracts shared by the local MCP stock tools.
- Create `mcp server/app/market_data/provider.py`: `MarketDataProvider` protocol plus deterministic local provider for tests and local development.
- Create `mcp server/app/market_data/__init__.py`: package exports.
- Modify `mcp server/app/main.py`: expose stock MCP tools through the existing stdio server.
- Modify `backend/app/intent/business_intents.json`: add A 股公司研究 local fallback business profiles.
- Modify `backend/app/graph/nodes/planner_agent.py`: route skill-backed task workflows into `skill_execution` instead of direct response.
- Modify `backend/app/graph/runtime.py`: execute non-RAG tool requests from Skill output before Retriever.
- Modify `backend/app/graph/nodes/tool_agent.py`: skip `rag_search`; Retriever owns RAG-to-evidence mapping.
- Create `backend/app/skills/admin_provider.py`: adapter from `admin/backend` Skill CRUD API to runtime Skill definition provider.
- Create `backend/app/skills/configured.py`: generic Skill executor for admin-configured skills.
- Modify `backend/app/skills/registry.py`: select configured skills from the remote/admin registry.
- Modify `backend/app/graph/nodes/skill_agent.py`: build registry from admin Skill provider when configured, with local fallback.
- Modify `backend/app/graph/nodes/analysis_agent.py`: include successful stock tool results in structured analysis.
- Modify `backend/app/graph/nodes/synthesizer.py`: render A 股研究 answer formats and append required disclaimer.
- Modify `backend/app/graph/nodes/verifier.py`: add Financial Verifier checks for investment-advice boundary and numeric source discipline.
- Add/update tests under `backend/app/tests/unit/` and `backend/app/tests/integration/`.
- Add docs update to `docs/intent-router.md`, `docs/tool-agent.md`, and `docs/synthesizer.md`.

---

### Task 1: Add Market Data Provider Contracts To MCP Server

**Files:**
- Create: `mcp server/app/market_data/__init__.py`
- Create: `mcp server/app/market_data/schemas.py`
- Create: `mcp server/app/market_data/provider.py`
- Test: `mcp server/tests/test_market_data_provider.py`

- [ ] **Step 1: Write the provider tests**

Create `mcp server/tests/test_market_data_provider.py`:

```python
from app.market_data.provider import FixtureMarketDataProvider


def test_fixture_provider_resolves_a_share_company_alias():
    provider = FixtureMarketDataProvider()

    result = provider.resolve_company("贵州茅台")

    assert result.symbol == "600519.SH"
    assert result.name == "贵州茅台"
    assert result.exchange == "SH"
    assert result.source_provider == "fixture"


def test_fixture_provider_returns_financial_metrics_with_source_metadata():
    provider = FixtureMarketDataProvider()

    result = provider.get_financial_metrics(symbol="600519.SH", period="2024")

    assert result.symbol == "600519.SH"
    assert result.period == "2024"
    assert result.metrics["revenue"] == 150560000000
    assert result.metrics["net_profit_parent"] == 74734000000
    assert result.source_provider == "fixture"
    assert result.as_of_date == "2025-04-03"


def test_fixture_provider_returns_empty_risk_events_for_unknown_symbol():
    provider = FixtureMarketDataProvider()

    result = provider.get_risk_events(symbol="000000.SZ", date_range="6m")

    assert result.symbol == "000000.SZ"
    assert result.events == []
    assert result.warnings == ["fixture_symbol_not_found"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest 'mcp server/tests/test_market_data_provider.py' -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.market_data'`.

- [ ] **Step 3: Add market data schemas**

Create `mcp server/app/market_data/schemas.py`:

```python
from typing import Any

from pydantic import BaseModel, Field


class CompanyResolution(BaseModel):
    query: str
    symbol: str
    name: str
    exchange: str
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class CompanyProfile(BaseModel):
    symbol: str
    name: str
    industry: str
    board: str
    main_business: str
    registered_address: str | None = None
    controller: str | None = None
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class MarketSnapshot(BaseModel):
    symbol: str
    price: float | None = None
    market_cap: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    pct_change: float | None = None
    turnover: float | None = None
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class FinancialMetrics(BaseModel):
    symbol: str
    period: str
    metrics: dict[str, float | int | None]
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class AnnouncementSearchResult(BaseModel):
    symbol: str
    date_range: str
    announcements: list[dict[str, Any]]
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)


class RiskEvents(BaseModel):
    symbol: str
    date_range: str
    events: list[dict[str, Any]]
    source_provider: str
    as_of_date: str
    warnings: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Add provider protocol and fixture implementation**

Create `mcp server/app/market_data/provider.py`:

```python
from typing import Protocol

from app.market_data.schemas import (
    AnnouncementSearchResult,
    CompanyProfile,
    CompanyResolution,
    FinancialMetrics,
    MarketSnapshot,
    RiskEvents,
)


FIXTURE_AS_OF_DATE = "2025-04-03"


class MarketDataProvider(Protocol):
    def resolve_company(self, query: str) -> CompanyResolution: ...

    def get_company_profile(self, symbol: str) -> CompanyProfile: ...

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot: ...

    def get_financial_metrics(self, symbol: str, period: str) -> FinancialMetrics: ...

    def search_announcements(self, symbol: str, date_range: str) -> AnnouncementSearchResult: ...

    def get_risk_events(self, symbol: str, date_range: str) -> RiskEvents: ...


class FixtureMarketDataProvider:
    def __init__(self) -> None:
        self._aliases = {
            "贵州茅台": "600519.SH",
            "茅台": "600519.SH",
            "600519": "600519.SH",
            "600519.SH": "600519.SH",
            "宁德时代": "300750.SZ",
            "300750": "300750.SZ",
            "300750.SZ": "300750.SZ",
            "隆基绿能": "601012.SH",
            "601012": "601012.SH",
            "601012.SH": "601012.SH",
        }
        self._profiles = {
            "600519.SH": {
                "name": "贵州茅台",
                "industry": "食品饮料",
                "board": "上交所主板",
                "main_business": "茅台酒及系列酒的生产与销售。",
                "registered_address": "贵州省仁怀市茅台镇",
                "controller": "贵州省国资委",
            },
            "300750.SZ": {
                "name": "宁德时代",
                "industry": "电力设备",
                "board": "深交所创业板",
                "main_business": "动力电池和储能电池系统研发、生产和销售。",
                "registered_address": "福建省宁德市",
                "controller": None,
            },
            "601012.SH": {
                "name": "隆基绿能",
                "industry": "电力设备",
                "board": "上交所主板",
                "main_business": "单晶硅片、电池组件及光伏解决方案。",
                "registered_address": "陕西省西安市",
                "controller": None,
            },
        }
        self._financials = {
            ("600519.SH", "2024"): {
                "revenue": 150560000000,
                "net_profit_parent": 74734000000,
                "gross_margin": 0.918,
                "roe": 0.342,
                "operating_cash_flow": 66800000000,
                "debt_to_asset": 0.155,
            },
            ("300750.SZ", "2024"): {
                "revenue": 362010000000,
                "net_profit_parent": 44121000000,
                "gross_margin": 0.263,
                "roe": 0.247,
                "operating_cash_flow": 92800000000,
                "debt_to_asset": 0.652,
            },
        }
        self._risk_events = {
            "601012.SH": [
                {
                    "event_type": "performance_pressure",
                    "date": "2025-03-20",
                    "summary": "光伏产业链价格下行导致盈利承压。",
                    "severity": "medium",
                }
            ]
        }

    def resolve_company(self, query: str) -> CompanyResolution:
        normalized_query = query.strip().upper()
        symbol = self._aliases.get(query.strip()) or self._aliases.get(normalized_query)
        if symbol is None:
            return CompanyResolution(
                query=query,
                symbol="",
                name="",
                exchange="",
                source_provider="fixture",
                as_of_date=FIXTURE_AS_OF_DATE,
                warnings=["fixture_company_not_found"],
            )
        profile = self._profiles[symbol]
        return CompanyResolution(
            query=query,
            symbol=symbol,
            name=profile["name"],
            exchange=symbol.split(".")[-1],
            source_provider="fixture",
            as_of_date=FIXTURE_AS_OF_DATE,
        )

    def get_company_profile(self, symbol: str) -> CompanyProfile:
        profile = self._profiles.get(symbol)
        if profile is None:
            return CompanyProfile(
                symbol=symbol,
                name="",
                industry="",
                board="",
                main_business="",
                source_provider="fixture",
                as_of_date=FIXTURE_AS_OF_DATE,
                warnings=["fixture_symbol_not_found"],
            )
        return CompanyProfile(
            symbol=symbol,
            source_provider="fixture",
            as_of_date=FIXTURE_AS_OF_DATE,
            **profile,
        )

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        return MarketSnapshot(
            symbol=symbol,
            price=1680.0 if symbol == "600519.SH" else None,
            market_cap=2110000000000 if symbol == "600519.SH" else None,
            pe_ttm=24.5 if symbol == "600519.SH" else None,
            pb=8.6 if symbol == "600519.SH" else None,
            pct_change=0.8 if symbol == "600519.SH" else None,
            turnover=3200000000 if symbol == "600519.SH" else None,
            source_provider="fixture",
            as_of_date=FIXTURE_AS_OF_DATE,
            warnings=[] if symbol in self._profiles else ["fixture_symbol_not_found"],
        )

    def get_financial_metrics(self, symbol: str, period: str) -> FinancialMetrics:
        metrics = self._financials.get((symbol, period), {})
        return FinancialMetrics(
            symbol=symbol,
            period=period,
            metrics=metrics,
            source_provider="fixture",
            as_of_date=FIXTURE_AS_OF_DATE,
            warnings=[] if metrics else ["fixture_financial_metrics_not_found"],
        )

    def search_announcements(self, symbol: str, date_range: str) -> AnnouncementSearchResult:
        announcements = [
            {
                "title": "2024 年年度报告",
                "date": "2025-03-29",
                "type": "annual_report",
                "document_id": f"announcement:{symbol}:2024-annual",
            }
        ] if symbol in self._profiles else []
        return AnnouncementSearchResult(
            symbol=symbol,
            date_range=date_range,
            announcements=announcements,
            source_provider="fixture",
            as_of_date=FIXTURE_AS_OF_DATE,
            warnings=[] if announcements else ["fixture_announcements_not_found"],
        )

    def get_risk_events(self, symbol: str, date_range: str) -> RiskEvents:
        events = self._risk_events.get(symbol, [])
        return RiskEvents(
            symbol=symbol,
            date_range=date_range,
            events=events,
            source_provider="fixture",
            as_of_date=FIXTURE_AS_OF_DATE,
            warnings=[] if symbol in self._profiles else ["fixture_symbol_not_found"],
        )
```

- [ ] **Step 5: Export package symbols**

Create `mcp server/app/market_data/__init__.py`:

```python
from app.market_data.provider import FixtureMarketDataProvider, MarketDataProvider

__all__ = ["FixtureMarketDataProvider", "MarketDataProvider"]
```

- [ ] **Step 6: Run the provider tests**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest 'mcp server/tests/test_market_data_provider.py' -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add 'mcp server/app/market_data' 'mcp server/tests/test_market_data_provider.py'
git commit -m "feat: add a-share market data provider contract"
```

---

### Task 2: Expose Stock MCP Tools

**Files:**
- Modify: `mcp server/app/main.py`
- Test: `backend/app/tests/unit/test_mcp_stdio_tool.py`

- [ ] **Step 1: Add failing MCP discovery test**

Append to `backend/app/tests/unit/test_mcp_stdio_tool.py`:

```python
def test_mcp_stdio_discovery_includes_a_share_stock_tools():
    server_root = Path(__file__).resolve().parents[4] / "mcp server"

    tools = discover_mcp_stdio_tools(
        command="/opt/anaconda3/bin/python3",
        args=["-m", "app.main"],
        cwd=str(server_root),
        server_name="mock",
        timeout_seconds=30.0,
    )
    specs = {tool.name: tool.spec for tool in tools}

    assert "mcp.mock.stock.resolve_company" in specs
    assert "mcp.mock.stock.company_profile" in specs
    assert "mcp.mock.stock.financial_metrics" in specs
    assert "mcp.mock.stock.market_snapshot" in specs
    assert "mcp.mock.stock.announcement_search" in specs
    assert "mcp.mock.stock.risk_events" in specs
    assert specs["mcp.mock.stock.resolve_company"].input_schema["required"] == ["query"]
```

Append another execution test:

```python
def test_dynamic_mcp_stdio_tool_executes_discovered_resolve_company_tool():
    server_root = Path(__file__).resolve().parents[4] / "mcp server"
    tools = discover_mcp_stdio_tools(
        command="/opt/anaconda3/bin/python3",
        args=["-m", "app.main"],
        cwd=str(server_root),
        server_name="mock",
        timeout_seconds=30.0,
    )
    tool = next(item for item in tools if item.name == "mcp.mock.stock.resolve_company")

    result = asyncio.run(tool.run(tool.input_model.model_validate({"query": "贵州茅台"})))

    assert result.status == "success"
    assert result.result["symbol"] == "600519.SH"
    assert result.result["name"] == "贵州茅台"
    assert result.result["source_provider"] == "fixture"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_mcp_stdio_tool.py::test_mcp_stdio_discovery_includes_a_share_stock_tools backend/app/tests/unit/test_mcp_stdio_tool.py::test_dynamic_mcp_stdio_tool_executes_discovered_resolve_company_tool -v
```

Expected: FAIL because stock tools are not exposed.

- [ ] **Step 3: Register stock tools in MCP server**

Modify `mcp server/app/main.py` to:

```python
from app.market_data.provider import FixtureMarketDataProvider
from app.tools import calculate_sum, echo
from mcp.server.fastmcp import FastMCP


mcp = FastMCP(
    name="bs-agent-local-mcp",
    instructions="本地 MCP Server，用于 BsAgent 调用真实 MCP tools 的联调验证。",
)
market_data_provider = FixtureMarketDataProvider()


@mcp.tool(name="echo")
def echo_tool(text: str) -> dict:
    """返回输入文本，用于验证 MCP 调用链路。"""
    return echo(text)


@mcp.tool(name="calculate_sum")
def calculate_sum_tool(numbers: list[float]) -> dict:
    """计算一组数字的总和。"""
    return calculate_sum(numbers)


@mcp.tool(name="stock.resolve_company")
def stock_resolve_company_tool(query: str) -> dict:
    """将 A 股公司名、简称或代码标准化为证券代码。"""
    return market_data_provider.resolve_company(query).model_dump()


@mcp.tool(name="stock.company_profile")
def stock_company_profile_tool(symbol: str) -> dict:
    """查询 A 股公司基础资料。"""
    return market_data_provider.get_company_profile(symbol).model_dump()


@mcp.tool(name="stock.market_snapshot")
def stock_market_snapshot_tool(symbol: str) -> dict:
    """查询 A 股行情和估值快照。"""
    return market_data_provider.get_market_snapshot(symbol).model_dump()


@mcp.tool(name="stock.financial_metrics")
def stock_financial_metrics_tool(symbol: str, period: str) -> dict:
    """查询 A 股公司指定报告期的核心财务指标。"""
    return market_data_provider.get_financial_metrics(symbol, period).model_dump()


@mcp.tool(name="stock.announcement_search")
def stock_announcement_search_tool(symbol: str, date_range: str = "12m") -> dict:
    """查询 A 股公告元数据。"""
    return market_data_provider.search_announcements(symbol, date_range).model_dump()


@mcp.tool(name="stock.risk_events")
def stock_risk_events_tool(symbol: str, date_range: str = "6m") -> dict:
    """查询 A 股公司风险事件元数据。"""
    return market_data_provider.get_risk_events(symbol, date_range).model_dump()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run MCP tests**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_mcp_stdio_tool.py -v
```

Expected: PASS. The older test assertion for exact tool set must be changed from equality to subset:

```python
assert {"mcp.mock.echo", "mcp.mock.calculate_sum"} <= set(specs)
```

- [ ] **Step 5: Commit**

```bash
git add 'mcp server/app/main.py' backend/app/tests/unit/test_mcp_stdio_tool.py
git commit -m "feat: expose a-share stock mcp tools"
```

---

### Task 3: Add A 股 Business Intents

**Files:**
- Modify: `backend/app/intent/business_intents.json`
- Modify: `backend/app/tests/unit/test_intent_router.py`

- [ ] **Step 1: Add failing intent tests**

Append to `backend/app/tests/unit/test_intent_router.py`:

```python
def test_hybrid_intent_router_detects_a_share_company_snapshot():
    router = HybridIntentRouter(catalog=BusinessIntentCatalog.load_default())

    decision = router.classify("分析一下贵州茅台", available_tools=[])

    assert decision.intent == "task_workflow"
    assert decision.domain == "a_share_equity_research"
    assert decision.sub_intent == "company_snapshot"
    assert decision.candidate_skill == "company_snapshot"
    assert decision.risk_level == "medium"


def test_hybrid_intent_router_detects_a_share_financial_report_analysis:
    router = HybridIntentRouter(catalog=BusinessIntentCatalog.load_default())

    decision = router.classify("分析宁德时代 2024 年报", available_tools=[])

    assert decision.intent == "task_workflow"
    assert decision.domain == "a_share_equity_research"
    assert decision.sub_intent == "financial_report_analysis"
    assert decision.candidate_skill == "financial_report_analysis"


def test_hybrid_intent_router_detects_a_share_risk_scan:
    router = HybridIntentRouter(catalog=BusinessIntentCatalog.load_default())

    decision = router.classify("看看隆基绿能最近有什么风险", available_tools=[])

    assert decision.intent == "task_workflow"
    assert decision.domain == "a_share_equity_research"
    assert decision.sub_intent == "risk_scan"
    assert decision.candidate_skill == "risk_scan"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_intent_router.py::test_hybrid_intent_router_detects_a_share_company_snapshot backend/app/tests/unit/test_intent_router.py::test_hybrid_intent_router_detects_a_share_financial_report_analysis backend/app/tests/unit/test_intent_router.py::test_hybrid_intent_router_detects_a_share_risk_scan -v
```

Expected: FAIL because the business profiles do not exist.

- [ ] **Step 3: Add business profiles**

Modify `backend/app/intent/business_intents.json` by adding these objects inside `profiles`:

```json
{
  "id": "company_research.company_snapshot",
  "intent": "task_workflow",
  "domain": "a_share_equity_research",
  "sub_intent": "company_snapshot",
  "candidate_skill": "company_snapshot",
  "candidate_tool": null,
  "risk_level": "medium",
  "trigger_keywords": ["分析一下", "基本面", "公司速览", "公司概览", "主营业务", "贵州茅台", "宁德时代"],
  "trigger_examples": ["分析一下贵州茅台", "帮我看一下 600519", "宁德时代现在基本面怎么样"],
  "required_capabilities": ["mcp_tool", "rag_retrieval", "financial_verifier"]
},
{
  "id": "company_research.financial_report_analysis",
  "intent": "task_workflow",
  "domain": "a_share_equity_research",
  "sub_intent": "financial_report_analysis",
  "candidate_skill": "financial_report_analysis",
  "candidate_tool": null,
  "risk_level": "medium",
  "trigger_keywords": ["年报", "季报", "财报", "利润", "现金流", "毛利率", "ROE"],
  "trigger_examples": ["分析宁德时代 2024 年报", "看看万华化学 2025Q1 财报有什么变化"],
  "required_capabilities": ["mcp_tool", "rag_retrieval", "financial_verifier"]
},
{
  "id": "company_research.risk_scan",
  "intent": "task_workflow",
  "domain": "a_share_equity_research",
  "sub_intent": "risk_scan",
  "candidate_skill": "risk_scan",
  "candidate_tool": null,
  "risk_level": "medium",
  "trigger_keywords": ["风险", "问询函", "处罚", "诉讼", "减持", "质押", "隆基绿能"],
  "trigger_examples": ["看看隆基绿能最近有什么风险", "这家公司有没有监管或诉讼风险"],
  "required_capabilities": ["mcp_tool", "rag_retrieval", "financial_verifier"]
}
```

- [ ] **Step 4: Run intent tests**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_intent_router.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/intent/business_intents.json backend/app/tests/unit/test_intent_router.py
git commit -m "feat: add a-share equity research intents"
```

---

### Task 4: Plan Skill-Backed Task Workflows

**Files:**
- Modify: `backend/app/graph/nodes/planner_agent.py`
- Modify: `backend/app/tests/unit/test_agent_nodes.py`

- [ ] **Step 1: Add failing planner test**

Append to `backend/app/tests/unit/test_agent_nodes.py`:

```python
def test_planner_agent_builds_skill_execution_plan_for_a_share_task_workflow():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下贵州茅台",
    )
    state["intent"] = {
        "intent": "task_workflow",
        "domain": "a_share_equity_research",
        "sub_intent": "company_snapshot",
        "candidate_skill": "company_snapshot",
        "risk_level": "medium",
        "needs_clarification": False,
        "confidence": 0.88,
        "reason_codes": ["business_profile:company_research.company_snapshot"],
        "next_node": "planner_agent",
    }

    result = build_planner_agent_node(available_tools=[])(state)

    assert result["plan"]["execution_mode"] == "skill_execution"
    assert result["plan"]["selected_skill"]["name"] == "company_snapshot"
    assert result["plan"]["response_policy"]["response_type"] == "equity_research_company_snapshot"
    assert result["plan"]["retrieval"]["required"] is True
```

- [ ] **Step 2: Run the planner test to verify it fails**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_agent_nodes.py::test_planner_agent_builds_skill_execution_plan_for_a_share_task_workflow -v
```

Expected: FAIL because `task_workflow` currently falls into `direct_response`.

- [ ] **Step 3: Add skill-backed workflow plan**

Modify `backend/app/graph/nodes/planner_agent.py`:

```python
def _build_plan(
    *,
    state: AgentState,
    llm_client: LlmClient | None,
    tool_specs: dict[str, ToolSpec],
) -> dict:
    intent = state["intent"] or {}
    intent_name = intent.get("intent", "knowledge_qa")

    if intent_name == "knowledge_qa":
        return _knowledge_qa_plan(intent)
    if intent_name == "tool_action":
        return _tool_action_plan(
            state=state,
            llm_client=llm_client,
            tool_specs=tool_specs,
        )
    if intent.get("candidate_skill"):
        return _skill_workflow_plan(intent)
    return _direct_response_plan(intent)
```

Add:

```python
def _skill_workflow_plan(intent: dict) -> dict:
    risk_level = intent.get("risk_level", "medium")
    candidate_skill = intent["candidate_skill"]
    sub_intent = intent.get("sub_intent") or "workflow"
    return {
        "objective": _objective_for_intent(intent.get("intent", "task_workflow")),
        "execution_mode": "skill_execution",
        "selected_skill": {
            "name": candidate_skill,
            "selection_reason": "intent_router_candidate",
        },
        "execution_steps": [
            "select_skill",
            "execute_structured_tools",
            "retrieve_evidence",
            "analyze_evidence_and_tool_results",
            "synthesize_answer",
            "verify_answer",
        ],
        "retrieval": {
            "required": True,
            "strategy": "rag_search",
            "evidence_policy": "answer_prefers_cited_evidence",
        },
        "tool_policy": {
            "allowed_tools": ["mcp.*", "rag_search"],
            "approval_required": risk_level == "high",
        },
        "response_policy": {
            "format": "equity_research_answer",
            "response_type": f"equity_research_{sub_intent}",
            "citation_required": False,
            "uncertainty": "explicit",
            "must_reference_user_message": True,
            "disclaimer_required": True,
        },
        "risk_controls": _risk_controls(intent),
    }
```

- [ ] **Step 4: Run planner tests**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_agent_nodes.py::test_planner_agent_builds_skill_execution_plan_for_a_share_task_workflow -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/graph/nodes/planner_agent.py backend/app/tests/unit/test_agent_nodes.py
git commit -m "feat: plan skill-backed equity workflows"
```

---

### Task 5: Connect Admin Skill Registry And ConfiguredSkill

**Files:**
- Create: `backend/app/skills/admin_provider.py`
- Create: `backend/app/skills/configured.py`
- Modify: `backend/app/skills/registry.py`
- Modify: `backend/app/graph/nodes/skill_agent.py`
- Modify: `backend/app/tests/unit/test_skill_registry_client.py`
- Modify: `backend/app/tests/unit/test_skill_registry.py`
- Modify: `backend/app/tests/unit/test_agent_nodes.py`

- [ ] **Step 1: Add failing admin provider and configured skill tests**

Append to `backend/app/tests/unit/test_skill_registry_client.py`:

```python
def test_admin_skill_provider_maps_admin_crud_api_to_runtime_definitions():
    from app.skills.admin_provider import AdminSkillDefinitionProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/skills":
            assert request.url.params["enabled"] == "true"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "name": "company_snapshot",
                            "version": "1.0.0",
                            "summary": "生成 A 股公司速览",
                            "supported_intents": ["task_workflow"],
                            "supported_domains": ["a_share_equity_research"],
                            "supported_sub_intents": ["company_snapshot"],
                            "allowed_tools": [
                                "mcp.mock.stock.resolve_company",
                                "mcp.mock.stock.company_profile",
                                "mcp.mock.stock.market_snapshot",
                                "mcp.mock.stock.announcement_search",
                                "rag_search",
                            ],
                            "risk_level": "medium",
                            "enabled": True,
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/skills/company_snapshot":
            return httpx.Response(
                200,
                json={
                    "name": "company_snapshot",
                    "version": "1.0.0",
                    "summary": "生成 A 股公司速览",
                    "supported_intents": ["task_workflow"],
                    "supported_domains": ["a_share_equity_research"],
                    "supported_sub_intents": ["company_snapshot"],
                    "allowed_tools": ["mcp.mock.stock.resolve_company", "rag_search"],
                    "risk_level": "medium",
                    "enabled": True,
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = httpx.Client(
        base_url="http://admin.test",
        transport=httpx.MockTransport(handler),
    )
    provider = AdminSkillDefinitionProvider(client=client)

    cards = provider.list_cards(tenant_id="tenant_1")
    definition = provider.get_definition(name="company_snapshot", version="1.0.0")
    guide = provider.get_execution_guide(name="company_snapshot", version="1.0.0")

    assert cards[0].name == "company_snapshot"
    assert cards[0].supported_intents == ["task_workflow"]
    assert definition.allowed_tools == ["mcp.mock.stock.resolve_company", "rag_search"]
    assert "a_share_equity_research" in definition.input_contract["supported_domains"]
    assert guide.required_evidence_policy == "answer_prefers_cited_evidence"
```

Append to `backend/app/tests/unit/test_agent_nodes.py`:

```python
def test_configured_skill_requests_admin_allowed_stock_tools_and_rag_search():
    from app.skills.configured import ConfiguredSkill
    from app.skills.definitions import SkillDefinition, SkillExecutionGuide

    skill = ConfiguredSkill(
        definition=SkillDefinition(
            name="company_snapshot",
            version="1.0.0",
            description="生成 A 股公司速览",
            input_contract={
                "supported_domains": ["a_share_equity_research"],
                "supported_sub_intents": ["company_snapshot"],
            },
            output_contract={},
            allowed_tools=[
                "mcp.mock.stock.resolve_company",
                "mcp.mock.stock.company_profile",
                "mcp.mock.stock.market_snapshot",
                "mcp.mock.stock.announcement_search",
                "rag_search",
            ],
            constraints=["必须追加投研免责声明"],
            failure_modes=[],
            definition_hash="hash_definition",
        ),
        guide=SkillExecutionGuide(
            name="company_snapshot",
            version="1.0.0",
            instructions=[],
            required_evidence_policy="answer_prefers_cited_evidence",
            definition_hash="hash_guide",
        ),
        risk_level="medium",
    )

    output = skill.run_validated(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下贵州茅台",
        context={"intent": {"sub_intent": "company_snapshot"}},
        evidence=None,
        memory_context=None,
    )
    request_names = [request.tool_name for request in output.requested_tools]

    assert output.skill_name == "company_snapshot"
    assert "mcp.mock.stock.resolve_company" in request_names
    assert "mcp.mock.stock.company_profile" in request_names
    assert "mcp.mock.stock.market_snapshot" in request_names
    assert "rag_search" in request_names
    assert output.requested_tools[1].arguments["symbol"] == "$resolved_symbol"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_skill_registry_client.py::test_admin_skill_provider_maps_admin_crud_api_to_runtime_definitions \
  backend/app/tests/unit/test_agent_nodes.py::test_configured_skill_requests_admin_allowed_stock_tools_and_rag_search -v
```

Expected: FAIL because `AdminSkillDefinitionProvider` and `ConfiguredSkill` do not exist.

- [ ] **Step 3: Add AdminSkillDefinitionProvider**

Create `backend/app/skills/admin_provider.py`:

```python
import hashlib
import json

import httpx

from app.skills.definitions import SkillCard, SkillDefinition, SkillExecutionGuide


class AdminSkillDefinitionProvider:
    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def list_cards(self, *, tenant_id: str) -> list[SkillCard]:
        response = self._client.get("/api/skills", params={"enabled": "true"})
        response.raise_for_status()
        items = response.json().get("items", [])
        return [
            SkillCard(
                name=item["name"],
                version=item["version"],
                summary=item["summary"],
                supported_intents=item.get("supported_intents", []),
                tags=[
                    *item.get("supported_domains", []),
                    *item.get("supported_sub_intents", []),
                ],
                risk_level=item.get("risk_level", "medium"),
                definition_hash=_definition_hash(item),
            )
            for item in items
        ]

    def get_definition(self, *, name: str, version: str) -> SkillDefinition:
        item = self._get_skill(name)
        return SkillDefinition(
            name=item["name"],
            version=item["version"],
            description=item["summary"],
            input_contract={
                "supported_intents": item.get("supported_intents", []),
                "supported_domains": item.get("supported_domains", []),
                "supported_sub_intents": item.get("supported_sub_intents", []),
            },
            output_contract={"type": "object"},
            allowed_tools=item.get("allowed_tools", []),
            constraints=["遵守 Skill 配置的 allowed_tools 和 risk_level"],
            failure_modes=["missing_required_tool", "tool_execution_failed", "insufficient_evidence"],
            definition_hash=_definition_hash(item),
        )

    def get_execution_guide(self, *, name: str, version: str) -> SkillExecutionGuide:
        item = self._get_skill(name)
        return SkillExecutionGuide(
            name=item["name"],
            version=item["version"],
            instructions=[
                "根据用户问题和 intent 生成 allowed_tools 中的工具调用请求。",
                "结构化金融数据只能通过 MCP stock tools 获取。",
                "非结构化证据通过 rag_search 获取。",
            ],
            few_shot_examples=[],
            recovery_policy=["工具不可用时降级说明", "证据不足时明确不确定性"],
            required_evidence_policy="answer_prefers_cited_evidence",
            definition_hash=_definition_hash(item),
        )

    def _get_skill(self, name: str) -> dict:
        response = self._client.get(f"/api/skills/{name}")
        response.raise_for_status()
        item = response.json()
        if item.get("enabled") is False:
            raise ValueError(f"Skill is disabled: {name}")
        return item


def _definition_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Add ConfiguredSkill**

Create `backend/app/skills/configured.py`:

```python
import re

from app.schemas.evidence import EvidencePackage
from app.schemas.memory import MemoryContext
from app.schemas.tools import ToolCallRequest
from app.skills.base import ApprovalPolicy, Skill, SkillOutput, SkillSpec
from app.skills.definitions import SkillDefinition, SkillExecutionGuide


RESOLVED_SYMBOL_PLACEHOLDER = "$resolved_symbol"


class ConfiguredSkill(Skill):
    def __init__(
        self,
        *,
        definition: SkillDefinition,
        guide: SkillExecutionGuide,
        risk_level: str = "medium",
    ) -> None:
        self.definition = definition
        self.guide = guide
        self.spec = SkillSpec(
            name=definition.name,
            version=definition.version,
            description=definition.description,
            supported_intents=definition.input_contract.get("supported_intents", []),
            required_inputs=["user_message"],
            optional_inputs=["evidence", "memory_context"],
            output_schema=definition.output_contract,
            risk_level=risk_level,
            approval_policy=ApprovalPolicy(require_human=risk_level == "high"),
            allowed_tools=definition.allowed_tools,
            eval_dataset=f"{definition.name}_golden",
        )

    def run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        user_message: str,
        context: dict,
        evidence: EvidencePackage | None,
        memory_context: MemoryContext | None,
    ) -> SkillOutput:
        requests = [
            _request_for_tool(
                tool_name=tool_name,
                user_message=user_message,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            for tool_name in self.definition.allowed_tools
        ]
        return SkillOutput(
            skill_name=self.definition.name,
            skill_version=self.definition.version,
            status="needs_retrieval" if any(request.tool_name == "rag_search" for request in requests) else "success",
            requested_tools=requests,
            draft_result={
                "definition_hash": self.definition.definition_hash,
                "guide_hash": self.guide.definition_hash,
            },
            reason_codes=["configured_skill_from_registry"],
        )


def _request_for_tool(
    *,
    tool_name: str,
    user_message: str,
    tenant_id: str,
    user_id: str,
) -> ToolCallRequest:
    query = _company_query(user_message)
    period = _period_from_message(user_message)
    if tool_name.endswith("stock.resolve_company"):
        return ToolCallRequest(
            tool_name=tool_name,
            arguments={"query": query},
            purpose="标准化 A 股公司代码",
        )
    if tool_name.endswith("stock.company_profile"):
        return ToolCallRequest(
            tool_name=tool_name,
            arguments={"symbol": RESOLVED_SYMBOL_PLACEHOLDER},
            purpose="获取公司基础资料",
        )
    if tool_name.endswith("stock.market_snapshot"):
        return ToolCallRequest(
            tool_name=tool_name,
            arguments={"symbol": RESOLVED_SYMBOL_PLACEHOLDER},
            purpose="获取行情和估值快照",
        )
    if tool_name.endswith("stock.financial_metrics"):
        return ToolCallRequest(
            tool_name=tool_name,
            arguments={"symbol": RESOLVED_SYMBOL_PLACEHOLDER, "period": period},
            purpose="获取结构化财务指标",
        )
    if tool_name.endswith("stock.announcement_search"):
        return ToolCallRequest(
            tool_name=tool_name,
            arguments={"symbol": RESOLVED_SYMBOL_PLACEHOLDER, "date_range": "12m"},
            purpose="搜索公告元数据",
        )
    if tool_name.endswith("stock.risk_events"):
        return ToolCallRequest(
            tool_name=tool_name,
            arguments={"symbol": RESOLVED_SYMBOL_PLACEHOLDER, "date_range": "6m"},
            purpose="获取风险事件",
        )
    if tool_name == "rag_search":
        return ToolCallRequest(
            tool_name="rag_search",
            tool_version="1.0.0",
            arguments={
                "query": user_message,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "top_k": 8,
                "retrieval_mode": "hybrid",
            },
            purpose="检索公司研究证据",
        )
    return ToolCallRequest(
        tool_name=tool_name,
        arguments={"query": user_message},
        purpose="执行配置型 Skill 工具调用",
    )


def _company_query(user_message: str) -> str:
    cleaned = user_message
    for marker in ("分析一下", "分析", "看看", "帮我看一下", "最近有什么风险"):
        cleaned = cleaned.replace(marker, "")
    cleaned = re.sub(r"20\d{2}\s*年?(?:Q[1-4]|一季报|半年报|三季报|年报)?", "", cleaned, flags=re.I)
    return cleaned.strip(" ，。?？") or user_message


def _period_from_message(user_message: str) -> str:
    match = re.search(r"(20\d{2})(?:\s*年)?(?:\s*(Q[1-4]|一季报|半年报|三季报|年报))?", user_message, re.I)
    if not match:
        return "2024"
    year = match.group(1)
    suffix = match.group(2)
    if not suffix or suffix == "年报":
        return year
    mapping = {"一季报": "Q1", "半年报": "H1", "三季报": "Q3"}
    return f"{year}{mapping.get(suffix, suffix.upper())}"
```

- [ ] **Step 5: Teach SkillRegistry to select configured skills**

Modify `backend/app/skills/registry.py`:

```python
from app.skills.base import Skill
from app.skills.configured import ConfiguredSkill
from app.skills.definitions import SkillCard, SkillDefinition, SkillExecutionGuide
from app.skills.registry_client import SkillDefinitionProvider
```

Replace `select` with:

```python
    def select(self, *, intent: str, candidate_skill: str | None) -> Skill:
        if candidate_skill and candidate_skill in self._skills:
            skill = self._skills[candidate_skill]
            if intent in skill.spec.supported_intents:
                return skill
            raise KeyError(
                f"Skill {candidate_skill} does not support intent: {intent}"
            )
        if candidate_skill and self._definition_provider is not None:
            definition, guide = self.get_definition_layers(name=candidate_skill, version="1.0.0")
            cards = self._definition_provider.list_cards(tenant_id="default")
            risk_level = next((card.risk_level for card in cards if card.name == candidate_skill), "medium")
            return ConfiguredSkill(definition=definition, guide=guide, risk_level=risk_level)
        for skill in self._skills.values():
            if intent in skill.spec.supported_intents:
                return skill
        raise KeyError(f"No skill registered for intent: {intent}")
```

This keeps existing local skills working while allowing admin-configured skills to be selected by `candidate_skill`.

- [ ] **Step 6: Build default registry from admin when configured**

Modify `backend/app/graph/nodes/skill_agent.py` imports:

```python
import httpx

from app.core.config import settings
from app.skills.admin_provider import AdminSkillDefinitionProvider
from app.skills.registry_client import CachedSkillDefinitionProvider
```

Modify `_default_skill_registry()`:

```python
def _default_skill_registry() -> SkillRegistry:
    if settings.skill_registry_base_url:
        client = httpx.Client(base_url=settings.skill_registry_base_url, timeout=5.0)
        provider = CachedSkillDefinitionProvider(
            AdminSkillDefinitionProvider(client=client),
            ttl_seconds=settings.skill_definition_cache_ttl_seconds,
        )
        remote_registry = SkillRegistry(definition_provider=provider)
        remote_registry.register(PolicyQaSkill())
        return remote_registry
    registry = SkillRegistry()
    registry.register(PolicyQaSkill())
    return registry
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_skill_registry_client.py::test_admin_skill_provider_maps_admin_crud_api_to_runtime_definitions \
  backend/app/tests/unit/test_agent_nodes.py::test_configured_skill_requests_admin_allowed_stock_tools_and_rag_search -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/skills/admin_provider.py backend/app/skills/configured.py backend/app/skills/registry.py backend/app/graph/nodes/skill_agent.py backend/app/tests/unit/test_skill_registry_client.py backend/app/tests/unit/test_skill_registry.py backend/app/tests/unit/test_agent_nodes.py
git commit -m "feat: run admin-configured skills"
```

---

### Task 6: Execute Skill Tool Requests Before RAG Retrieval

**Files:**
- Modify: `backend/app/graph/runtime.py`
- Modify: `backend/app/graph/nodes/tool_agent.py`
- Modify: `backend/app/tests/integration/test_graph_runtime.py`

- [ ] **Step 1: Add failing graph routing test**

Append to `backend/app/tests/integration/test_graph_runtime.py`:

```python
def test_question_answer_graph_runs_stock_tools_before_rag_for_company_snapshot():
    trace_store = InMemoryTraceStore()

    def fake_retriever(state):
        assert any(
            call.request.tool_name == "mcp.mock.stock.resolve_company"
            for call in state["tool_calls"]
        )
        state["evidence"] = EvidencePackage(
            evidence_package_id="evp_1",
            query_set=["贵州茅台 公司研究"],
            coverage="sufficient",
            items=[
                EvidenceItem(
                    evidence_id="ev_1",
                    chunk_id="chunk_1",
                    document_id="doc_1",
                    title="贵州茅台 2024 年报",
                    content="贵州茅台主营茅台酒及系列酒，2024 年经营保持稳健。",
                    score=0.9,
                )
            ],
        )
        return state

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下贵州茅台",
        retriever_node=fake_retriever,
        trace_store=trace_store,
    )

    node_names = [event.node_name for event in trace_store.events]
    assert "tool_agent" in node_names
    assert "retriever_agent" in node_names
    assert node_names.index("tool_agent") < node_names.index("retriever_agent")
    assert result["selected_skill"]["skill_name"] == "company_snapshot"
    assert any(
        call.request.tool_name == "mcp.mock.stock.company_profile"
        and call.request.arguments["symbol"] == "600519.SH"
        for call in result["tool_calls"]
    )
```

- [ ] **Step 2: Run the graph test to verify it fails**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_graph_runtime.py::test_question_answer_graph_runs_stock_tools_before_rag_for_company_snapshot -v
```

Expected: FAIL because the graph routes `skill_agent` directly to `retriever_agent`.

- [ ] **Step 3: Skip RAG requests and hydrate resolved stock symbol inside Tool Agent**

Modify `backend/app/graph/nodes/tool_agent.py`:

```python
RESOLVED_SYMBOL_PLACEHOLDER = "$resolved_symbol"


def _requested_tool_calls(state: AgentState) -> list[ToolCallRequest]:
    plan = state["plan"] or {}
    requested_tools = plan.get("requested_tools", [])
    parsed_requests = [
        request
        if isinstance(request, ToolCallRequest)
        else ToolCallRequest.model_validate(request)
        for request in requested_tools
    ]
    return [
        request
        for request in parsed_requests
        if request.tool_name != "rag_search"
    ]


def _hydrate_request_arguments(
    *,
    request: ToolCallRequest,
    resolved_symbol: str | None,
) -> ToolCallRequest:
    if resolved_symbol is None:
        return request
    arguments = dict(request.arguments)
    if arguments.get("symbol") == RESOLVED_SYMBOL_PLACEHOLDER:
        arguments["symbol"] = resolved_symbol
        return request.model_copy(update={"arguments": arguments})
    return request


def _resolved_symbol_from_record(record: ToolCallRecord) -> str | None:
    if not record.request.tool_name.endswith("stock.resolve_company"):
        return None
    if record.result is None or record.result.status != "success" or not record.result.result:
        return None
    symbol = record.result.result.get("symbol")
    return symbol or None
```

Modify `_execute_requests` to execute sequentially and hydrate later stock tool calls:

```python
async def _execute_requests(
    *,
    requests: list[ToolCallRequest],
    registry: ToolRegistry,
    trace_id: str,
) -> list[ToolCallRecord]:
    executor = ToolAgentExecutor(registry=registry)
    records: list[ToolCallRecord] = []
    resolved_symbol: str | None = None
    for request in requests:
        hydrated_request = _hydrate_request_arguments(
            request=request,
            resolved_symbol=resolved_symbol,
        )
        result = await executor.execute(hydrated_request, trace_id=trace_id)
        if not result.trace_id:
            result.trace_id = trace_id
        record = ToolCallRecord(request=hydrated_request, result=result)
        records.append(record)
        resolved_symbol = resolved_symbol or _resolved_symbol_from_record(record)
    return records
```

- [ ] **Step 4: Route skill execution through Tool Agent before Retriever**

Modify `backend/app/graph/runtime.py` graph edges so skill execution goes:

```python
graph.add_edge("skill_agent", "tool_agent")
graph.add_edge("retriever_agent", "analysis_agent")
```

Keep planner conditional routing for direct `tool_execution` to `tool_agent`; after `tool_agent`, route to `synthesizer` only when `plan.execution_mode == "tool_execution"`. Add helper:

```python
def _route_after_tool_agent(state: AgentState) -> str:
    plan = state["plan"] or {}
    if plan.get("execution_mode") == "tool_execution":
        return "synthesizer"
    return "retriever_agent"
```

Use:

```python
graph.add_conditional_edges(
    "tool_agent",
    _route_after_tool_agent,
    {
        "synthesizer": "synthesizer",
        "retriever_agent": "retriever_agent",
    },
)
```

Remove any unconditional `graph.add_edge("tool_agent", "synthesizer")`.

- [ ] **Step 5: Run graph routing tests**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_graph_runtime.py::test_question_answer_graph_runs_stock_tools_before_rag_for_company_snapshot backend/app/tests/integration/test_graph_runtime.py::test_question_answer_graph_routes_short_sum_expression_to_tool_agent -v
```

Expected: PASS. The sum tool path must still go directly from `tool_agent` to `synthesizer`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/graph/runtime.py backend/app/graph/nodes/tool_agent.py backend/app/tests/integration/test_graph_runtime.py
git commit -m "feat: execute skill tool requests before rag retrieval"
```

---

### Task 7: Include Structured Tool Results In Analysis And Synthesis

**Files:**
- Modify: `backend/app/graph/nodes/analysis_agent.py`
- Modify: `backend/app/graph/nodes/synthesizer.py`
- Modify: `backend/app/tests/unit/test_agent_nodes.py`

- [ ] **Step 1: Add failing analysis and synthesis tests**

Append to `backend/app/tests/unit/test_agent_nodes.py`:

```python
def test_analysis_agent_collects_successful_stock_tool_results():
    state = create_initial_state("s1", "t1", "u1", "分析一下贵州茅台")
    state["tool_calls"] = [
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.mock.stock.company_profile",
                arguments={"symbol": "600519.SH"},
                purpose="获取公司基础资料",
            ),
            result={
                "tool_call_id": "tool_1",
                "tool_name": "mcp.mock.stock.company_profile",
                "status": "success",
                "result": {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "industry": "食品饮料",
                    "main_business": "茅台酒及系列酒的生产与销售。",
                    "source_provider": "fixture",
                    "as_of_date": "2025-04-03",
                    "warnings": [],
                },
                "trace_id": "trace_1",
            },
        )
    ]

    result = analysis_agent_node(state)

    assert result["analysis"]["structured_tool_results"][0]["tool_name"] == "mcp.mock.stock.company_profile"
    assert result["analysis"]["structured_tool_results"][0]["result"]["symbol"] == "600519.SH"


def test_synthesizer_adds_financial_disclaimer_for_equity_research_answer():
    state = create_initial_state("s1", "t1", "u1", "分析一下贵州茅台")
    state["intent"] = {"domain": "a_share_equity_research", "sub_intent": "company_snapshot"}
    state["plan"] = {
        "execution_mode": "skill_execution",
        "response_policy": {
            "response_type": "equity_research_company_snapshot",
            "citation_required": False,
            "disclaimer_required": True,
        },
    }
    state["analysis"] = {
        "findings": ["贵州茅台主营茅台酒及系列酒。"],
        "structured_tool_results": [
            {
                "tool_name": "mcp.mock.stock.company_profile",
                "result": {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "industry": "食品饮料",
                    "main_business": "茅台酒及系列酒的生产与销售。",
                },
            }
        ],
        "missing_information": [],
        "confidence": 0.8,
    }

    result = synthesizer_node(state)

    assert "公司概览" in result["draft_answer"]["content"]
    assert "贵州茅台" in result["draft_answer"]["content"]
    assert "不构成投资建议" in result["draft_answer"]["content"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_agent_nodes.py::test_analysis_agent_collects_successful_stock_tool_results backend/app/tests/unit/test_agent_nodes.py::test_synthesizer_adds_financial_disclaimer_for_equity_research_answer -v
```

Expected: FAIL because structured stock tool results are not collected or rendered.

- [ ] **Step 3: Extend Analysis Agent**

Modify `backend/app/graph/nodes/analysis_agent.py`:

```python
from app.graph.state import AgentState


def _successful_stock_tool_results(state: AgentState) -> list[dict]:
    results = []
    for call in state["tool_calls"]:
        if not call.request.tool_name.startswith("mcp.mock.stock."):
            continue
        if call.result is None or call.result.status != "success":
            continue
        results.append(
            {
                "tool_name": call.request.tool_name,
                "purpose": call.request.purpose,
                "result": call.result.result or {},
            }
        )
    return results


def analysis_agent_node(state: AgentState) -> AgentState:
    evidence = state["evidence"]
    structured_tool_results = _successful_stock_tool_results(state)
    if evidence is None or not evidence.items:
        state["analysis"] = {
            "findings": [],
            "structured_tool_results": structured_tool_results,
            "missing_information": [] if structured_tool_results else ["没有可用证据"],
            "confidence": 0.6 if structured_tool_results else 0.0,
        }
        return state

    state["analysis"] = {
        "findings": [item.content for item in evidence.items],
        "structured_tool_results": structured_tool_results,
        "missing_information": [],
        "confidence": max(item.score for item in evidence.items),
    }
    return state
```

- [ ] **Step 4: Add equity research renderer**

Modify `backend/app/graph/nodes/synthesizer.py` inside `build_synthesizer_node`, before the normal evidence branch:

```python
        if response_policy.get("response_type", "").startswith("equity_research_"):
            content = _render_equity_research_content(state=state, response_policy=response_policy)
            state["draft_answer"] = {
                "content": _finalize_response_content(content, state["memory_context"]),
                "claims": [],
                "cited_evidence_ids": [
                    item.evidence_id for item in evidence.items
                ] if evidence is not None else [],
                "missing_information": missing_information,
                "assumptions": [],
                "confidence": analysis.get("confidence", 0.6),
                "response_policy": response_policy,
            }
            return state
```

Add helper:

```python
FINANCIAL_DISCLAIMER = "以上内容仅用于投研辅助分析，不构成投资建议、买卖建议或收益承诺。"


def _render_equity_research_content(*, state: AgentState, response_policy: dict) -> str:
    analysis = state["analysis"] or {}
    intent = state["intent"] or {}
    structured = analysis.get("structured_tool_results", [])
    findings = analysis.get("findings", [])
    profile = _first_stock_tool_result(structured, "company_profile")
    metrics = _first_stock_tool_result(structured, "financial_metrics")
    risks = _first_stock_tool_result(structured, "risk_events")
    response_type = response_policy.get("response_type")
    if response_type == "equity_research_financial_report_analysis":
        body = _financial_report_template(profile=profile, metrics=metrics, findings=findings)
    elif response_type == "equity_research_risk_scan":
        body = _risk_scan_template(profile=profile, risks=risks, findings=findings)
    else:
        body = _company_snapshot_template(profile=profile, metrics=metrics, findings=findings)
    if intent.get("domain") == "a_share_equity_research" and FINANCIAL_DISCLAIMER not in body:
        body = f"{body}\n\n免责声明\n{FINANCIAL_DISCLAIMER}"
    return body


def _first_stock_tool_result(structured: list[dict], suffix: str) -> dict:
    for item in structured:
        if item.get("tool_name", "").endswith(suffix):
            return item.get("result") or {}
    return {}


def _company_snapshot_template(*, profile: dict, metrics: dict, findings: list[str]) -> str:
    name = profile.get("name") or "该公司"
    business = profile.get("main_business") or "当前结构化资料未返回主营业务。"
    industry = profile.get("industry") or "未返回行业分类"
    evidence_text = "；".join(findings[:3]) if findings else "当前 RAG 未返回足够正文证据。"
    return (
        "公司概览\n"
        f"{name}，行业：{industry}。\n\n"
        "核心业务\n"
        f"{business}\n\n"
        "关键财务表现\n"
        f"{metrics.get('metrics', {}) if metrics else '当前未返回结构化财务指标。'}\n\n"
        "近期重要事件\n"
        f"{evidence_text}\n\n"
        "主要风险点\n"
        "需要结合公告、财报和行业景气度继续验证。\n\n"
        "待进一步验证的问题\n"
        "后续应继续核对最新公告、财务指标变化和行业数据。"
    )


def _financial_report_template(*, profile: dict, metrics: dict, findings: list[str]) -> str:
    metric_payload = metrics.get("metrics", {}) if metrics else {}
    evidence_text = "；".join(findings[:3]) if findings else "当前 RAG 未返回足够财报正文证据。"
    return (
        "摘要结论\n"
        f"{profile.get('name', '该公司')} 财报分析需要结合结构化指标和公告正文。\n\n"
        "收入与利润变化\n"
        f"{metric_payload}\n\n"
        "盈利质量\n"
        "重点关注毛利率、ROE 和扣非利润变化。\n\n"
        "现金流质量\n"
        "重点关注经营现金流与净利润的匹配度。\n\n"
        "异常点\n"
        f"{evidence_text}"
    )


def _risk_scan_template(*, profile: dict, risks: dict, findings: list[str]) -> str:
    events = risks.get("events", []) if risks else []
    evidence_text = "；".join(findings[:3]) if findings else "当前 RAG 未返回足够风险证据。"
    return (
        "风险总览\n"
        f"{profile.get('name', '该公司')} 当前结构化风险事件数量：{len(events)}。\n\n"
        "经营风险\n"
        f"{events}\n\n"
        "监管/合规风险\n"
        f"{evidence_text}\n\n"
        "后续关注点\n"
        "继续跟踪公告、问询函、处罚、诉讼、减持和质押变化。"
    )
```

- [ ] **Step 5: Run tests**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_agent_nodes.py::test_analysis_agent_collects_successful_stock_tool_results backend/app/tests/unit/test_agent_nodes.py::test_synthesizer_adds_financial_disclaimer_for_equity_research_answer -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/graph/nodes/analysis_agent.py backend/app/graph/nodes/synthesizer.py backend/app/tests/unit/test_agent_nodes.py
git commit -m "feat: synthesize equity research answers"
```

---

### Task 8: Add Financial Verifier Rules

**Files:**
- Modify: `backend/app/graph/nodes/verifier.py`
- Modify: `backend/app/tests/unit/test_agent_nodes.py`

- [ ] **Step 1: Add failing verifier tests**

Append to `backend/app/tests/unit/test_agent_nodes.py`:

```python
def test_financial_verifier_rejects_buy_recommendation():
    state = create_initial_state("s1", "t1", "u1", "贵州茅台能买吗")
    state["intent"] = {"domain": "a_share_equity_research"}
    state["plan"] = {"response_policy": {"citation_required": False, "disclaimer_required": True}}
    state["draft_answer"] = {
        "content": "建议买入贵州茅台，长期稳赚。",
        "claims": [],
        "cited_evidence_ids": [],
        "missing_information": [],
        "assumptions": [],
        "confidence": 0.8,
        "response_policy": {"citation_required": False},
    }

    result = verifier_node(state)

    assert result["verification"]["verdict"] == "needs_revision"
    assert "investment_advice_boundary_violation" in result["verification"]["risk_flags"]
    assert result["verification"]["required_action"] == "revise_answer"


def test_financial_verifier_requires_disclaimer_for_equity_research():
    state = create_initial_state("s1", "t1", "u1", "分析一下贵州茅台")
    state["intent"] = {"domain": "a_share_equity_research"}
    state["plan"] = {"response_policy": {"citation_required": False, "disclaimer_required": True}}
    state["draft_answer"] = {
        "content": "贵州茅台主营白酒。",
        "claims": [],
        "cited_evidence_ids": [],
        "missing_information": [],
        "assumptions": [],
        "confidence": 0.8,
        "response_policy": {"citation_required": False},
    }

    result = verifier_node(state)

    assert result["verification"]["verdict"] == "needs_revision"
    assert "missing_financial_disclaimer" in result["verification"]["risk_flags"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_agent_nodes.py::test_financial_verifier_rejects_buy_recommendation backend/app/tests/unit/test_agent_nodes.py::test_financial_verifier_requires_disclaimer_for_equity_research -v
```

Expected: FAIL because Verifier has no financial policy.

- [ ] **Step 3: Add financial policy checks before generic pass checks**

Modify `backend/app/graph/nodes/verifier.py`:

```python
FINANCIAL_DISCLAIMER_TEXT = "不构成投资建议"
INVESTMENT_ADVICE_PATTERNS = (
    "建议买入",
    "建议卖出",
    "可以买入",
    "应该买入",
    "满仓",
    "梭哈",
    "稳赚",
    "保证收益",
)


def _financial_risk_flags(state: AgentState) -> list[str]:
    intent = state["intent"] or {}
    if intent.get("domain") != "a_share_equity_research":
        return []
    draft_answer = state["draft_answer"] or {}
    content = draft_answer.get("content", "")
    flags = []
    if any(pattern in content for pattern in INVESTMENT_ADVICE_PATTERNS):
        flags.append("investment_advice_boundary_violation")
    if (state["plan"] or {}).get("response_policy", {}).get("disclaimer_required") and FINANCIAL_DISCLAIMER_TEXT not in content:
        flags.append("missing_financial_disclaimer")
    if "目标价" in content and not draft_answer.get("cited_evidence_ids") and not state["tool_calls"]:
        flags.append("unsupported_target_price")
    return flags
```

At the top of `verifier_node`, after local variables are initialized:

```python
    financial_flags = _financial_risk_flags(state)
    if financial_flags:
        state["verification"] = {
            "verdict": "needs_revision",
            "faithfulness_score": 0.0,
            "citation_errors": [],
            "risk_flags": financial_flags,
            "required_action": "revise_answer",
            "finance_policy_violations": financial_flags,
        }
        return state
```

- [ ] **Step 4: Run verifier tests**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_agent_nodes.py::test_financial_verifier_rejects_buy_recommendation backend/app/tests/unit/test_agent_nodes.py::test_financial_verifier_requires_disclaimer_for_equity_research -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/graph/nodes/verifier.py backend/app/tests/unit/test_agent_nodes.py
git commit -m "feat: add financial verifier guardrails"
```

---

### Task 9: Add End-To-End A 股 Company Snapshot Test

**Files:**
- Modify: `backend/app/tests/integration/test_graph_runtime.py`
- Modify: `backend/app/tests/integration/test_session_api.py`

- [ ] **Step 1: Add graph integration test**

Append to `backend/app/tests/integration/test_graph_runtime.py`:

```python
def test_question_answer_graph_completes_a_share_company_snapshot():
    trace_store = InMemoryTraceStore()

    def fake_retriever(state):
        state["evidence"] = EvidencePackage(
            evidence_package_id="evp_1",
            query_set=["贵州茅台 年报 公司研究"],
            coverage="sufficient",
            items=[
                EvidenceItem(
                    evidence_id="ev_1",
                    chunk_id="chunk_1",
                    document_id="doc_1",
                    title="贵州茅台 2024 年报",
                    content="贵州茅台主营茅台酒及系列酒，收入结构以茅台酒为核心。",
                    score=0.92,
                )
            ],
        )
        return state

    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下贵州茅台",
        retriever_node=fake_retriever,
        trace_store=trace_store,
    )

    content = result["final_response"]["content"]
    assert result["intent"]["domain"] == "a_share_equity_research"
    assert result["selected_skill"]["skill_name"] == "company_snapshot"
    assert any(call.request.tool_name == "mcp.mock.stock.resolve_company" for call in result["tool_calls"])
    assert "公司概览" in content
    assert "贵州茅台" in content
    assert "不构成投资建议" in content
```

- [ ] **Step 2: Run the integration test**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_graph_runtime.py::test_question_answer_graph_completes_a_share_company_snapshot -v
```

Expected: PASS after Tasks 1-8.

- [ ] **Step 3: Add SSE API integration assertion**

Modify `backend/app/tests/integration/test_session_api.py` by adding a stream test that posts:

```python
payload = {
    "tenant_id": "tenant_1",
    "user_id": "user_1",
    "message": "分析一下贵州茅台",
}
```

Assert the SSE body contains:

```python
assert "event: trace_events" in body
assert "公司概览" in body
assert "不构成投资建议" in body
```

- [ ] **Step 4: Run API test**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_session_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/tests/integration/test_graph_runtime.py backend/app/tests/integration/test_session_api.py
git commit -m "test: cover a-share company snapshot flow"
```

---

### Task 10: Update Documentation And Run Full Verification

**Files:**
- Modify: `docs/intent-router.md`
- Modify: `docs/tool-agent.md`
- Modify: `docs/synthesizer.md`
- Modify: `docs/superpowers/specs/2026-07-03-a-share-equity-research-agent-design.md` only if implementation decisions changed.

- [ ] **Step 1: Update docs**

Add to `docs/intent-router.md`:

```markdown
## A 股公司研究意图

第一版支持 `a_share_equity_research` domain：

- `company_snapshot`
- `financial_report_analysis`
- `risk_scan`

这些意图通常输出 `task_workflow`，并通过 `candidate_skill` 进入 Skill Agent。
```

Add to `docs/tool-agent.md`:

```markdown
## 金融 MCP 工具

A 股结构化数据通过 MCP stdio 动态发现为内部 `ToolSpec`。Agent 不直接访问财经 API。

第一版工具：

- `mcp.mock.stock.resolve_company`
- `mcp.mock.stock.company_profile`
- `mcp.mock.stock.market_snapshot`
- `mcp.mock.stock.financial_metrics`
- `mcp.mock.stock.announcement_search`
- `mcp.mock.stock.risk_events`

`rag_search` 仍由 Retriever Agent 执行并映射为 `EvidencePackage`，Tool Agent 会跳过 `rag_search` 请求。
```

Add to `docs/synthesizer.md`:

```markdown
## A 股研究输出边界

A 股公司研究回答必须追加免责声明：

> 以上内容仅用于投研辅助分析，不构成投资建议、买卖建议或收益承诺。

短答案和工具结果回答不使用 Markdown 加粗，不暴露 MCP 工具内部名称或 LLM provider。
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_mcp_stdio_tool.py \
  backend/app/tests/unit/test_intent_router.py \
  backend/app/tests/unit/test_agent_nodes.py \
  backend/app/tests/integration/test_graph_runtime.py \
  backend/app/tests/integration/test_session_api.py -v
```

Expected: PASS.

- [ ] **Step 3: Run ruff**

Run:

```bash
/opt/anaconda3/bin/python3 -m ruff check backend/app
```

Expected: `All checks passed!`

- [ ] **Step 4: Run live local SSE smoke**

Start backend if needed:

```bash
cd /Users/myfolder/前端学习项目/BsAgent/backend
/opt/anaconda3/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then call:

```bash
curl -N -s -X POST http://127.0.0.1:8000/api/sessions/a_share_smoke/messages/stream \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"tenant_id":"tenant_1","user_id":"user_1","message":"分析一下贵州茅台"}'
```

Expected SSE contains:

```text
event: trace_events
公司概览
贵州茅台
不构成投资建议
```

- [ ] **Step 5: Commit**

```bash
git add docs/intent-router.md docs/tool-agent.md docs/synthesizer.md docs/superpowers/specs/2026-07-03-a-share-equity-research-agent-design.md
git commit -m "docs: document a-share equity research flow"
```

---

## Self-Review

Spec coverage:

- Product positioning and non-goals are covered by Task 8 and Task 10 docs.
- RAG plus structured data split is covered by Task 1, Task 2, Task 6, and Task 7.
- Three MVP skills are represented as admin-configured Skill records and executed by the generic `ConfiguredSkill` in Task 5.
- MCP stock tools are covered by Task 2.
- Skill-backed workflow routing is covered by Task 4 and Task 6.
- Financial Verifier is covered by Task 8.
- Trace and SSE verification are covered by Task 9 and Task 10.
- Tests and eval starter coverage are represented by focused unit, integration, and smoke tests in Tasks 1-10.

Known intentional constraint:

- The first implementation uses a deterministic fixture provider inside the local MCP server. This keeps the Agent graph and contracts testable. A real AkShare or Tushare adapter should be a follow-up plan once dependency installation and data-source credentials are decided.
