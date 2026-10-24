from typing import ClassVar, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from app.schemas.tools import ToolCallResult, ToolError
from app.search.deepseek import WebSearchProviderError
from app.search.schemas import EvidenceCoverage, SearchResearchContext, WebSearchResponse
from app.tools.base import Tool

WEB_SEARCH_ERROR_MESSAGES = {
    "web_search_timeout": "联网搜索服务响应超时。",
    "web_search_http_error": "联网搜索服务请求失败。",
    "web_search_unavailable": "联网搜索服务暂不可用。",
    "web_search_response_invalid": "联网搜索服务返回了无效响应。",
    "web_search_response_incomplete": "联网搜索会话未完成。",
    "web_search_no_results": "联网搜索未返回可验证来源。",
}


class WebSearchClient(Protocol):
    async def search(
        self, *, query: str, max_results: int, research_context: SearchResearchContext | None = None,
    ) -> WebSearchResponse: ...


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空。")
        return normalized


class WebSearchTool(Tool[WebSearchInput]):
    name = "web_search"
    version = "1.0.0"
    input_model = WebSearchInput
    description = (
        "联网检索带来源的公开信息，用于当日市场涨跌原因、政策、新闻、公告、行业动态、"
        "事实核验和公司实体关系等需要新鲜或外部证据的投研任务。"
        "仅补充公开资料，搜索成功不代表已取得原接口完整数据或满足计算条件；"
        "用户或固定流程限定来源时不得自行替换。权限失败后的来源切换须走用户确认，不自动兜底。"
    )
    permission_scope: ClassVar[list[str]] = ["web:search"]
    risk_level = "low"
    timeout_ms = 30_000
    output_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "answer": {"type": "string"},
            "coverage": EvidenceCoverage.model_json_schema(),
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string", "format": "uri"},
                        "snippet": {"type": "string"},
                        "published_at": {"type": ["string", "null"]},
                    },
                    "required": ["title", "url", "snippet", "published_at"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["query", "answer", "results"],
        "additionalProperties": False,
    }

    def __init__(self, *, client: WebSearchClient) -> None:
        self._client = client

    async def run(self, input_data: WebSearchInput) -> ToolCallResult:
        return await self._run(input_data)

    async def run_with_context(self, input_data: WebSearchInput, *, context) -> ToolCallResult:
        return await self._run(input_data, research_context=context.research_context)

    async def _run(
        self, input_data: WebSearchInput, *, research_context: SearchResearchContext | None = None,
    ) -> ToolCallResult:
        try:
            kwargs = {"query": input_data.query, "max_results": input_data.max_results}
            if research_context is not None:
                kwargs["research_context"] = research_context
            response = await self._client.search(**kwargs)
        except WebSearchProviderError as exc:
            code = exc.code if exc.code in WEB_SEARCH_ERROR_MESSAGES else "web_search_unavailable"
            return ToolCallResult(
                tool_call_id=f"tool_{uuid4().hex}",
                tool_name=self.name,
                status="failed",
                error=ToolError(
                    code=code,
                    message=WEB_SEARCH_ERROR_MESSAGES[code],
                    retryable=exc.retryable,
                ),
                trace_id=f"trace_{uuid4().hex}",
            )

        return ToolCallResult(
            tool_call_id=f"tool_{uuid4().hex}",
            tool_name=self.name,
            status="success",
            result={
                "query": response.query,
                "answer": response.answer,
                "results": [item.model_dump(mode="json") for item in response.results],
                **({"coverage": (response.coverage or EvidenceCoverage(status="unknown")).model_dump(mode="json")}
                   if research_context is not None or response.coverage is not None else {}),
            },
            trace_id=f"trace_{uuid4().hex}",
        )
