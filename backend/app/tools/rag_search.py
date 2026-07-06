from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from app.schemas.tools import ToolCallResult, ToolError
from app.tools.base import Tool


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
    permission_scope = ["rag:retrieve"]
    risk_level = "low"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def run(self, input_data: RagSearchInput) -> ToolCallResult:
        try:
            response = await self._client.post(
                "/api/rag/retrieve",
                json=_rag_retrieve_payload(input_data),
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
