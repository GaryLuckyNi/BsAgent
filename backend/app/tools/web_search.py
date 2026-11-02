import json
from hashlib import sha256
from typing import ClassVar
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from app.core.config import settings
from app.schemas.tools import ToolCallResult, ToolError
from app.search.base import WEB_SEARCH_ERROR_MESSAGES, WebSearchClient, WebSearchProviderError
from app.search.factory import build_web_search_client
from app.search.schemas import EvidenceCoverage, SearchResearchContext
from app.tools.base import Tool
from app.tools.registry import ExecutionBinding


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=128)
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("query", mode="before")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空。")
        return normalized


class WebSearchTool(Tool[WebSearchInput]):
    name = "web_search"
    version = "2.0.0"
    input_model = WebSearchInput
    description = (
        "联网检索带来源的公开信息，用于当日市场涨跌原因、政策、新闻、公告、行业动态、"
        "事实核验和公司实体关系等需要新鲜或外部证据的投研任务。"
        "仅补充公开资料，搜索成功不代表已取得原接口完整数据或满足计算条件；"
        "普通研究任务可根据可用性选择来源；普通只读取证可预先生成跨来源备用调用，联网搜索始终作为最后备用，专业来源成功后不执行搜索。"
        "固定Skill的指定工具与搜索许可仍遵守其冻结契约。"
        "query 最多128字符。来源摘要是外部检索材料；answer 可能为空，coverage 未知时须依据来源判断目标覆盖。"
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

    def __init__(self, *, client: WebSearchClient, timeout_seconds: float = 30.0) -> None:
        self._client = client
        self.timeout_ms = int(timeout_seconds * 1000)

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


def build_default_web_search_registration() -> tuple[WebSearchTool, ExecutionBinding]:
    client = build_web_search_client()
    tool = WebSearchTool(client=client, timeout_seconds=settings.web_search_timeout_seconds)
    direct = settings.web_search_provider == "deepseek_search_api"
    identity = {
        "provider": settings.web_search_provider,
        "base_url": (settings.web_search_base_url if direct else settings.deepseek_base_url)
        .rstrip("/"),
        "timeout_seconds": settings.web_search_timeout_seconds,
        **({"model": settings.deepseek_model} if not direct else {}),
    }
    revision = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return tool, ExecutionBinding(
        provider="local", implementation_id=f"{type(client).__module__}.{type(client).__qualname__}",
        deployment_revision=tool.version, config_revision=revision,
    )
