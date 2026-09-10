import json

from langchain_core.messages import AIMessage, ToolMessage
import numpy as np
from pydantic import BaseModel

from app.graph.runtime import run_question_answer_graph
from app.llm.schemas import LlmCompletionResult
from app.schemas.tools import ToolCallResult
from app.tools.base import Tool
from app.tools.registry import ToolRegistry
from app.tools.search import ToolSearchIndex, ToolSearchService


class WorkflowLlm:
    async def complete(self, **kwargs):
        prompt = kwargs["system_prompt"]
        if "投研任务理解器" in prompt:
            content = json.dumps(
                {
                    "intent_type": "financial_research",
                    "domain": "a_share_equity_research",
                    "subdomain": "company_snapshot",
                    "goal": "分析海能达的经营质量",
                    "confidence": 0.95,
                },
                ensure_ascii=False,
            )
        elif "投研任务 Planner" in prompt:
            content = json.dumps(
                {
                    "tasks": [
                        {
                            "id": "quality",
                            "task_goal": "基于已有上下文评估经营质量",
                            "depends_on": [],
                            "skill_name": None,
                            "tool_name": None,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        elif "投研任务执行器" in prompt:
            content = "当前没有外部经营数据，无法判断经营质量。"
        elif "投研报告合成器" in prompt:
            content = "## 分析结论\n\n现有数据不足，暂时无法判断海能达的经营质量。"
        else:
            raise AssertionError(prompt)
        return LlmCompletionResult(content=content, model="fake", provider="fake")


class MarketDataInput(BaseModel):
    symbol: str


class MarketDataTool(Tool[MarketDataInput]):
    name = "mcp.tushare.stock.daily"
    version = "1.0.0"
    description = "根据证券代码查询 A 股日线行情"
    input_model = MarketDataInput

    async def run(self, input_data: MarketDataInput) -> ToolCallResult:
        return ToolCallResult(
            tool_call_id="provider-call",
            tool_name=self.name,
            status="success",
            result={"symbol": input_data.symbol, "close": 8.12, "pct_chg": 1.5},
            trace_id="provider-trace",
        )


class ZeroEmbedder:
    def embed_documents(self, texts):
        return np.zeros((len(texts), 2), dtype=np.float32)

    def embed_query(self, _text):
        return np.zeros(2, dtype=np.float32)


class BoundMarketModel:
    def invoke(self, _messages):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "model-call",
                    "name": "mcp_tushare_stock_daily",
                    "args": {"symbol": "002583.SZ"},
                    "type": "tool_call",
                }
            ],
        )


class ToolWorkflowLlm:
    def bind_tools(self, tools, *, tool_choice):
        assert tools[0]["function"]["name"] == "mcp_tushare_stock_daily"
        assert tool_choice == "mcp_tushare_stock_daily"
        return BoundMarketModel()

    async def complete(self, **kwargs):
        prompt = kwargs["system_prompt"]
        if "投研任务理解器" in prompt:
            content = json.dumps(
                {
                    "intent_type": "financial_research",
                    "domain": "a_share_equity_research",
                    "subdomain": "company_snapshot",
                    "goal": "查询海能达最新行情并解读涨跌",
                    "confidence": 0.98,
                },
                ensure_ascii=False,
            )
        elif "投研任务 Planner" in prompt:
            disclosed_tools = json.loads(kwargs["user_prompt"])["tools"]
            assert {item["name"] for item in disclosed_tools} == {
                MarketDataTool.name
            }
            content = json.dumps(
                {
                    "tasks": [
                        {
                            "id": "market_data",
                            "task_goal": "查询海能达最新日线行情",
                            "depends_on": [],
                            "skill_name": None,
                            "tool_name": MarketDataTool.name,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        elif "投研报告合成器" in prompt:
            task_results = json.loads(kwargs["user_prompt"])["task_results"]
            assert '"close":8.12' in task_results[0]["content"]
            content = "## 行情解读\n\n海能达收盘价为 8.12 元，当日上涨 1.5%。"
        else:
            raise AssertionError(prompt)
        return LlmCompletionResult(content=content, model="fake", provider="fake")


def _market_tool_runtime():
    registry = ToolRegistry()
    registry.register(MarketDataTool())
    snapshot = registry.snapshot()
    index = ToolSearchIndex.build(
        specs=snapshot.specs(),
        catalog_digest=snapshot.catalog_digest,
        embedder=ZeroEmbedder(),
    )
    search_service = ToolSearchService(
        registry=snapshot,
        index=index,
        candidate_k=20,
    )
    return snapshot, search_service


def test_full_graph_executes_dynamic_analysis_task(business_intent_catalog):
    registry = ToolRegistry().snapshot()

    result = run_question_answer_graph(
        business_intent_catalog=business_intent_catalog,
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析海能达的经营质量",
        skill_agent_node=lambda state: state,
        tool_registry_factory=lambda: registry,
        llm_client=WorkflowLlm(),
    )

    assert result["task_results"][0]["task_id"] == "quality"
    assert result["task_results"][0]["status"] == "success"
    assert result["verification"]["verdict"] == "pass"
    assert "## 分析结论" in result["final_response"]["content"]
    assert "不构成投资建议" in result["final_response"]["content"]


def test_full_graph_discovers_and_executes_dynamic_tool_task(
    business_intent_catalog,
):
    registry, search_service = _market_tool_runtime()

    result = run_question_answer_graph(
        business_intent_catalog=business_intent_catalog,
        session_id="session-tool",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="查询海能达最新行情并解读涨跌",
        skill_agent_node=lambda state: state,
        tool_registry_factory=lambda: registry,
        tool_search_service=search_service,
        llm_client=ToolWorkflowLlm(),
    )

    assert result["task_results"][0]["status"] == "success"
    assert any(isinstance(message, ToolMessage) for message in result["messages"])
    assert "## 行情解读" in result["final_response"]["content"]
    assert "不构成投资建议" in result["final_response"]["content"]
