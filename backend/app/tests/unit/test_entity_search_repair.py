import json

import pytest
from pydantic import BaseModel

from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult
from app.schemas.tools import ToolCallRequest, ToolCallResult
from app.tools.base import Tool
from app.tools.registry import ToolRegistry


SOURCE_URL = "https://www.sse.com.cn/disclosure/longxin"


class RecordingLlmClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def complete(self, **kwargs) -> LlmCompletionResult:
        self.calls.append(kwargs)
        return LlmCompletionResult(
            content=json.dumps(self.payload, ensure_ascii=False),
            model="test-model",
            provider="test",
        )


class ResolveInput(BaseModel):
    query: str


class ProfileInput(BaseModel):
    symbol: str


class SearchInput(BaseModel):
    query: str
    max_results: int = 5


def _registry_factory(
    *,
    resolve_queries: list[str],
    search_queries: list[str],
    profile_calls: list[str],
    search_answer: str | None = None,
    search_result_url: str = SOURCE_URL,
) -> ToolRegistry:
    class ResolveCompanyTool(Tool[ResolveInput]):
        name = "mcp.mock.stock.resolve_company"
        version = "1.0.0"
        input_model = ResolveInput

        async def run(self, input_data: ResolveInput) -> ToolCallResult:
            resolve_queries.append(input_data.query)
            if input_data.query in {"长鑫科技", "688825.SH"}:
                result = {
                    "query": input_data.query,
                    "symbol": "688825.SH",
                    "name": "长鑫科技",
                }
            else:
                result = {
                    "query": input_data.query,
                    "symbol": "",
                    "name": "",
                    "warnings": ["company_not_found"],
                }
            return ToolCallResult(
                tool_call_id="tool_resolve",
                tool_name=self.name,
                status="success",
                result=result,
                trace_id="trace_tool",
            )

    class WebSearchTool(Tool[SearchInput]):
        name = "web_search"
        version = "1.0.0"
        input_model = SearchInput

        async def run(self, input_data: SearchInput) -> ToolCallResult:
            search_queries.append(input_data.query)
            return ToolCallResult(
                tool_call_id="tool_search",
                tool_name=self.name,
                status="success",
                result={
                    "query": input_data.query,
                    "answer": search_answer
                    or "公开披露显示长鑫存储与长鑫科技为明确上市主体关系。",
                    "results": [
                        {
                            "title": "长鑫科技首次公开发行公告",
                            "url": search_result_url,
                            "snippet": "证券代码 688825",
                            "published_at": "2026-05-27",
                        }
                    ],
                },
                trace_id="trace_tool",
            )

    class CompanyProfileTool(Tool[ProfileInput]):
        name = "mcp.mock.stock.company_profile"
        version = "1.0.0"
        input_model = ProfileInput

        async def run(self, input_data: ProfileInput) -> ToolCallResult:
            profile_calls.append(input_data.symbol)
            return ToolCallResult(
                tool_call_id="tool_profile",
                tool_name=self.name,
                status="success",
                result={"symbol": input_data.symbol, "name": "长鑫科技"},
                trace_id="trace_tool",
            )

    registry = ToolRegistry()
    registry.register(ResolveCompanyTool())
    registry.register(WebSearchTool())
    registry.register(CompanyProfileTool())
    return registry


def test_tool_agent_uses_structured_search_answer_without_second_llm_call():
    resolve_queries: list[str] = []
    search_queries: list[str] = []
    profile_calls: list[str] = []
    structured_answer = json.dumps(
        {
            "status": "resolved",
            "candidate_query": "长鑫科技",
            "canonical_name": "长鑫科技",
            "symbol": "688825",
            "relation_type": "listed_subject",
            "confidence": "high",
            "source_urls": [SOURCE_URL, "https://untrusted.example/not-returned"],
            "reason": "公开发行文件支持该上市主体关系",
        },
        ensure_ascii=False,
    ) + "\n\n以上为 JSON 格式，非解释。"

    def registry_factory():
        return _registry_factory(
            resolve_queries=resolve_queries,
            search_queries=search_queries,
            profile_calls=profile_calls,
            search_answer=structured_answer,
            search_result_url=(
                f"{SOURCE_URL.replace('www.', '')}?utm_source=search#result"
            ),
        )

    llm_client = RecordingLlmClient({"status": "unresolved"})
    result = build_tool_agent_node(
        registry_factory=registry_factory,
        llm_client=llm_client,
    )(_state(registry_factory))

    assert resolve_queries == ["长鑫存储", "长鑫科技"]
    assert profile_calls == ["688825.SH"]
    assert llm_client.calls == []
    assert result["plan"]["tool_repair"]["candidate_source"] == "web_search"
    assert result["plan"]["tool_repair"]["source_urls"] == [SOURCE_URL]


def _state(registry_factory) -> dict:
    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下长鑫存储",
    )
    state["permission_scopes"] = ["web:search"]
    state["plan"] = {
        "tool_policy": {
            "allowed_tools": [
                "mcp.mock.stock.resolve_company",
                "mcp.mock.stock.company_profile",
            ]
        },
        "requested_tools": [
            ToolCallRequest(
                tool_name="mcp.mock.stock.resolve_company",
                arguments={"query": "长鑫存储"},
                purpose="解析公司简称",
            ),
            ToolCallRequest(
                tool_name="mcp.mock.stock.company_profile",
                arguments={"symbol": "$resolved_symbol"},
                purpose="查询公司资料",
            ),
        ],
    }
    state["tool_catalog_digest"] = registry_factory().catalog_digest
    return state


def test_tool_agent_searches_before_repair_and_revalidates_discovered_entity():
    resolve_queries: list[str] = []
    search_queries: list[str] = []
    profile_calls: list[str] = []

    def registry_factory():
        return _registry_factory(
            resolve_queries=resolve_queries,
            search_queries=search_queries,
            profile_calls=profile_calls,
        )

    llm_client = RecordingLlmClient(
        {
            "status": "resolved",
            "candidate_query": "长鑫科技",
            "canonical_name": "长鑫科技",
            "symbol": "688825.SH",
            "relation_type": "listed_subject",
            "confidence": 0.96,
            "source_urls": [SOURCE_URL],
            "reason": "公开发行文件支持该上市主体关系",
        }
    )

    result = build_tool_agent_node(
        registry_factory=registry_factory,
        llm_client=llm_client,
    )(_state(registry_factory))

    assert resolve_queries == ["长鑫存储", "长鑫科技"]
    assert len(search_queries) == 1
    assert "长鑫存储" in search_queries[0]
    assert "上市主体" in search_queries[0]
    assert profile_calls == ["688825.SH"]
    assert [record.request.tool_name for record in result["tool_calls"]] == [
        "mcp.mock.stock.resolve_company",
        "web_search",
        "mcp.mock.stock.resolve_company",
        "mcp.mock.stock.company_profile",
    ]
    repair_prompt = json.loads(llm_client.calls[0]["user_prompt"])
    assert "不可信外部内容" in llm_client.calls[0]["system_prompt"]
    assert repair_prompt["failed_query"] == "长鑫存储"
    assert repair_prompt["search_evidence"]["results"][0]["url"] == SOURCE_URL
    assert result["plan"]["tool_repair"]["strategy"] == (
        "web_search_entity_discovery_and_market_revalidation"
    )
    assert result["plan"]["tool_repair"]["source_urls"] == [SOURCE_URL]


def test_tool_agent_rejects_search_backed_peer_company_without_market_retry():
    resolve_queries: list[str] = []
    search_queries: list[str] = []
    profile_calls: list[str] = []

    def registry_factory():
        return _registry_factory(
            resolve_queries=resolve_queries,
            search_queries=search_queries,
            profile_calls=profile_calls,
        )

    llm_client = RecordingLlmClient(
        {
            "status": "resolved",
            "candidate_query": "晶合集成",
            "canonical_name": "晶合集成",
            "symbol": "688249.SH",
            "relation_type": "peer_company",
            "confidence": 0.99,
            "source_urls": [SOURCE_URL],
            "reason": "同属半导体行业，但不是同一实体",
        }
    )

    result = build_tool_agent_node(
        registry_factory=registry_factory,
        llm_client=llm_client,
    )(_state(registry_factory))

    assert resolve_queries == ["长鑫存储"]
    assert len(search_queries) == 1
    assert profile_calls == []
    assert result["plan"]["tool_repair"]["status"] == "not_recovered"
    assert result["plan"]["tool_repair"]["reason"] == "unsafe_entity_relation"
    assert result["errors"][-1]["code"] == "company_not_found"


def test_tool_agent_rejects_entity_source_not_returned_by_search():
    resolve_queries: list[str] = []
    search_queries: list[str] = []
    profile_calls: list[str] = []

    def registry_factory():
        return _registry_factory(
            resolve_queries=resolve_queries,
            search_queries=search_queries,
            profile_calls=profile_calls,
        )

    llm_client = RecordingLlmClient(
        {
            "status": "resolved",
            "candidate_query": "长鑫科技",
            "canonical_name": "长鑫科技",
            "symbol": "688825.SH",
            "relation_type": "listed_subject",
            "confidence": 0.96,
            "source_urls": ["https://untrusted.example/not-in-search"],
            "reason": "来源并未出现在搜索结果中",
        }
    )

    result = build_tool_agent_node(
        registry_factory=registry_factory,
        llm_client=llm_client,
    )(_state(registry_factory))

    assert resolve_queries == ["长鑫存储"]
    assert profile_calls == []
    assert result["plan"]["tool_repair"]["reason"] == "unverified_entity_sources"


def test_tool_agent_rejects_market_revalidation_symbol_mismatch():
    resolve_queries: list[str] = []
    search_queries: list[str] = []
    profile_calls: list[str] = []

    def registry_factory():
        return _registry_factory(
            resolve_queries=resolve_queries,
            search_queries=search_queries,
            profile_calls=profile_calls,
        )

    llm_client = RecordingLlmClient(
        {
            "status": "resolved",
            "candidate_query": "长鑫科技",
            "canonical_name": "长鑫科技",
            "symbol": "688249.SH",
            "relation_type": "listed_subject",
            "confidence": 0.96,
            "source_urls": [SOURCE_URL],
            "reason": "搜索摘要中的代码与市场数据源冲突",
        }
    )

    result = build_tool_agent_node(
        registry_factory=registry_factory,
        llm_client=llm_client,
    )(_state(registry_factory))

    assert resolve_queries == ["长鑫存储", "长鑫科技"]
    assert profile_calls == []
    assert result["plan"]["tool_repair"]["reason"] == "market_revalidation_failed"


@pytest.mark.parametrize(
    ("candidate_overrides", "expected_reason"),
    [
        ({"confidence": 0.79}, "entity_confidence_too_low"),
        (
            {
                "status": "unresolved",
                "candidate_query": "",
                "relation_type": "unknown",
                "source_urls": [],
            },
            "entity_unresolved",
        ),
    ],
)
def test_tool_agent_rejects_low_confidence_or_ambiguous_search_entity(
    candidate_overrides,
    expected_reason,
):
    resolve_queries: list[str] = []
    search_queries: list[str] = []
    profile_calls: list[str] = []

    def registry_factory():
        return _registry_factory(
            resolve_queries=resolve_queries,
            search_queries=search_queries,
            profile_calls=profile_calls,
        )

    candidate = {
        "status": "resolved",
        "candidate_query": "长鑫科技",
        "canonical_name": "长鑫科技",
        "symbol": "688825.SH",
        "relation_type": "listed_subject",
        "confidence": 0.96,
        "source_urls": [SOURCE_URL],
        "reason": "搜索证据不足以安全进入复验",
        **candidate_overrides,
    }
    result = build_tool_agent_node(
        registry_factory=registry_factory,
        llm_client=RecordingLlmClient(candidate),
    )(_state(registry_factory))

    assert resolve_queries == ["长鑫存储"]
    assert profile_calls == []
    assert result["plan"]["tool_repair"]["reason"] == expected_reason
