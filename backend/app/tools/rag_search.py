import re
from typing import Any, ClassVar, Literal
from uuid import uuid4

import httpx
from opentelemetry.propagate import inject
from pydantic import BaseModel, Field

from app.core.config import settings
from app.schemas.tools import ToolCallResult, ToolError
from app.security.http_response import HttpResponseBudgetError, async_bounded_request
from app.security.run_authorization import refresh_tool_context
from app.tools.base import Tool
from app.tools.registry import ExecutionBinding

_BUSINESS_TRACE_ID_HEADER = "x-business-trace-id"
_SAFE_BUSINESS_TRACE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class RagSearchError(RuntimeError):
    def __init__(self, *, code: str, message: str, http_status: int) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(message)


class RagAuthorizationError(RagSearchError):
    def __init__(self, code="rag_authorization_unavailable", http_status=503):
        super().__init__(code=code, message=code, http_status=http_status)


class RagSearchInput(BaseModel):
    query: str
    tenant_id: str = ""
    user_id: str = ""
    top_k: int = Field(default=8, gt=0)
    filters: dict[str, Any] | None = None
    retrieval_mode: Literal["semantic", "hybrid", "keyword"] = "hybrid"
    rerank: bool = True
    query_rewrite: bool = True
    graph_rag: bool = False


class RagChunk(BaseModel):
    chunk_id: str
    document_id: str
    source_uri: str | None = None
    title: str | None = None
    content: str
    score: float = Field(ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RagSearchOutput(BaseModel):
    results: list[RagChunk]
    query_used: str
    total: int = Field(ge=0)
    latency_ms: int = Field(ge=0)


class RagRetrieveResponse(BaseModel):
    contexts: list[dict[str, Any]]
    query: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RagSearchTool(Tool[RagSearchInput]):
    name = "rag_search"
    version = "1.0.0"
    input_model = RagSearchInput
    description = "检索当前用户所属企业的内部知识库，仅查询本企业已接入的内部文档、制度、流程及业务资料。适用于用户明确要求查询本企业内部资料、内部知识库，或上下文已明确资料来自内部知识库的请求。不用于查询外部公司的公开资料、公告、行情或研报。用户仅提到某家公司、“知识证据”或“分析依据”，不代表需要调用本工具。本工具不具备互联网搜索或任意公司信息查询能力。"
    permission_scope: ClassVar[list[str]] = ["rag:retrieve"]
    risk_level = "low"
    effect_type = "read_only"
    server_injected_fields: ClassVar[list[str]] = ["tenant_id", "user_id"]
    requires_explicit_execution_binding = True

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if client is None and base_url is None:
            raise ValueError("RagSearchTool requires client or base_url.")
        self._client = client
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    async def run(self, input_data: RagSearchInput) -> ToolCallResult:
        if settings.rbac_backend == "principal":
            raise RagAuthorizationError("rag_authorization_context_required", 403)
        return await self._run(input_data, business_trace_id=None)

    async def run_with_context(
        self,
        input_data: RagSearchInput,
        *,
        context: Any,
    ) -> ToolCallResult:
        if context is None:
            raise RagAuthorizationError("rag_authorization_context_required", 403)
        if (input_data.tenant_id and input_data.tenant_id != context.tenant_id) or (
            input_data.user_id and input_data.user_id != context.user_id
        ):
            raise RagAuthorizationError("rag_context_identity_mismatch", 403)
        if settings.rbac_backend == "principal":
            context = refresh_tool_context(context)
            if "rag:retrieve" not in context.permission_scopes:
                raise RagAuthorizationError("rag_authorization_denied", 403)
        input_data = input_data.model_copy(
            update={"tenant_id": context.tenant_id, "user_id": context.user_id}
        )
        return await self._run(
            input_data,
            business_trace_id=getattr(context, "business_trace_id", None),
        )

    async def _run(
        self,
        input_data: RagSearchInput,
        *,
        business_trace_id: str | None,
    ) -> ToolCallResult:
        headers = _propagation_headers(business_trace_id)
        if self._client is not None:
            return await self._run_with_client(
                self._client,
                input_data,
                headers=headers,
            )
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            follow_redirects=False,
        ) as client:
            return await self._run_with_client(client, input_data, headers=headers)

    async def _run_with_client(
        self,
        client: httpx.AsyncClient,
        input_data: RagSearchInput,
        *,
        headers: dict[str, str],
    ) -> ToolCallResult:
        failure = None
        try:
            response = await async_bounded_request(
                client, "POST", "/api/rag/retrieve", json=_rag_retrieve_payload(input_data), headers=headers,
                timeout_seconds=self._timeout_seconds,
            )
        except HttpResponseBudgetError as error:
            expired = error.code == "http_response_deadline"
            failure = RagSearchError(
                code="rag_service_timeout" if expired else "rag_response_limit_exceeded",
                message="检索服务响应超时。" if expired else "检索服务响应超出读取限制。",
                http_status=504 if expired else 502,
            )
        except httpx.TimeoutException:
            failure = RagSearchError(
                code="rag_service_timeout",
                message="检索服务响应超时。",
                http_status=504,
            )
        except httpx.RequestError:
            failure = RagSearchError(
                code="rag_service_unreachable",
                message="检索服务不可达。",
                http_status=502,
            )
        if failure is not None:
            raise failure

        if response.status_code == 404:
            return ToolCallResult(
                tool_call_id=f"tool_{uuid4().hex}",
                tool_name=self.name,
                status="failed",
                result={
                    "results": [],
                    "query_used": input_data.query,
                    "total": 0,
                    "latency_ms": 0,
                },
                error=ToolError(code="rag_no_evidence", message="未检索到相关证据。"),
                latency_ms=0,
                trace_id=f"trace_{uuid4().hex}",
            )
        if response.status_code != 200:
            raise RagSearchError(
                code=_rag_error_code(response.status_code),
                message="检索服务返回非预期响应。",
                http_status=response.status_code if response.status_code >= 400 else 502,
            )

        try:
            output = RagSearchOutput.model_validate(_normalize_rag_retrieve_response(response.json()))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
            failure = RagSearchError(code="rag_response_invalid", message="检索服务返回了无效响应。", http_status=502)
        if failure is not None:
            raise failure
        return ToolCallResult(
            tool_call_id=f"tool_{uuid4().hex}",
            tool_name=self.name,
            status="success",
            result=output.model_dump(),
            latency_ms=output.latency_ms,
            trace_id=f"trace_{uuid4().hex}",
        )


def build_default_rag_search_registration(
    *,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
) -> tuple[RagSearchTool, ExecutionBinding]:
    resolved_base_url = base_url or settings.rag_base_url
    return (
        RagSearchTool(
            base_url=resolved_base_url,
            timeout_seconds=timeout_seconds or settings.rag_timeout_seconds,
        ),
        ExecutionBinding(
            provider="rag",
            implementation_id="rag.http.retrieve",
            server_id=resolved_base_url,
            deployment_revision=settings.rag_provider_revision,
            config_revision=settings.rag_config_revision,
        ),
    )


def _rag_retrieve_payload(input_data: RagSearchInput) -> dict[str, Any]:
    return {
        "query": input_data.query,
        "filters": input_data.filters or {},
        "retrieval": {
            "topK": input_data.top_k,
            "contextLimit": input_data.top_k,
            "hybrid": input_data.retrieval_mode == "hybrid",
            "rerank": input_data.rerank,
            "queryRewrite": input_data.query_rewrite,
            "graphRag": input_data.graph_rag,
        },
        "responseMode": {
            "includeText": True,
            "includeGraph": True,
            "includeScores": True,
            "includeDebug": False,
        },
    }


def _propagation_headers(business_trace_id: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    inject(headers)
    if isinstance(business_trace_id, str) and _SAFE_BUSINESS_TRACE_ID.fullmatch(business_trace_id):
        headers[_BUSINESS_TRACE_ID_HEADER] = business_trace_id
    return headers


def _rag_error_code(status_code: int) -> str:
    if status_code == 503:
        return "rag_index_unavailable"
    if status_code == 502:
        return "rag_upstream_error"
    return "rag_http_error"


def _normalize_rag_retrieve_response(payload: dict[str, Any]) -> dict[str, Any]:
    response = RagRetrieveResponse.model_validate(payload)
    contexts = response.contexts
    return {
        "results": [
            {
                "chunk_id": context["id"],
                "document_id": context["source"]["sourcePath"],
                "source_uri": context["source"]["sourcePath"],
                "title": context["source"].get("title"),
                "content": context["text"],
                "score": context.get("score", {}).get("final", 0.0),
                "metadata": context.get("metadata", {}),
            }
            for context in contexts
        ],
        "query_used": response.query.get("standalone", ""),
        "total": response.metadata.get("contextCount", len(contexts)),
        "latency_ms": response.metadata.get("durationMs", 0),
    }
