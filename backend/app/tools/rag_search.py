import re
from typing import Any, ClassVar, Literal
from uuid import uuid4

import httpx
from opentelemetry.propagate import inject
from pydantic import BaseModel, Field

from app.core.config import settings
from app.schemas.tools import ToolCallResult, ToolError
from app.tools.base import Tool
from app.tools.registry import ExecutionBinding

_BUSINESS_TRACE_ID_HEADER = "x-business-trace-id"
_SAFE_BUSINESS_TRACE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class RagSearchError(RuntimeError):
    def __init__(self, *, code: str, message: str, http_status: int) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(message)


class RagSearchInput(BaseModel):
    query: str
    tenant_id: str
    user_id: str
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
    description = "检索企业知识库证据片段"
    permission_scope: ClassVar[list[str]] = ["rag:retrieve"]
    risk_level = "low"
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
        return await self._run(input_data, business_trace_id=None)

    async def run_with_context(
        self,
        input_data: RagSearchInput,
        *,
        context: Any,
    ) -> ToolCallResult:
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
        ) as client:
            return await self._run_with_client(client, input_data, headers=headers)

    async def _run_with_client(
        self,
        client: httpx.AsyncClient,
        input_data: RagSearchInput,
        *,
        headers: dict[str, str],
    ) -> ToolCallResult:
        try:
            response = await client.post(
                "/api/rag/retrieve",
                json=_rag_retrieve_payload(input_data),
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise RagSearchError(
                code="rag_service_timeout",
                message=str(exc),
                http_status=504,
            ) from exc
        except httpx.RequestError as exc:
            raise RagSearchError(
                code="rag_service_unreachable",
                message=str(exc),
                http_status=502,
            ) from exc

        if response.status_code == 404:
            message = _response_error_message(response)
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
                error=ToolError(code="rag_no_evidence", message=message),
                latency_ms=0,
                trace_id=f"trace_{uuid4().hex}",
            )
        if response.status_code >= 400:
            raise RagSearchError(
                code=_rag_error_code(response.status_code),
                message=_response_error_message(response),
                http_status=response.status_code,
            )

        response.raise_for_status()

        output = RagSearchOutput.model_validate(_normalize_rag_retrieve_response(response.json()))
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


def _response_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return str(detail or response.reason_phrase)


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
