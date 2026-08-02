from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from app.schemas.tools import ToolCallResult, ToolError
from app.search.deepseek import WebSearchProviderError
from app.search.schemas import WebSearchResponse
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
    async def search(self, *, query: str, max_results: int) -> WebSearchResponse: ...


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
    description = "联网检索公开网页，返回带来源的实体发现证据"
    permission_scope = ["web:search"]
    risk_level = "low"
    timeout_ms = 30_000
    output_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "answer": {"type": "string"},
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
        try:
            response = await self._client.search(
                query=input_data.query,
                max_results=input_data.max_results,
            )
        except WebSearchProviderError as exc:
            code = (
                exc.code
                if exc.code in WEB_SEARCH_ERROR_MESSAGES
                else "web_search_unavailable"
            )
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
                "results": [
                    item.model_dump(mode="json") for item in response.results
                ],
            },
            trace_id=f"trace_{uuid4().hex}",
        )
