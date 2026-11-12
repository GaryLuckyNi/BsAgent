"""直连 DeepSeek Search API，只获取来源，不进行额外模型生成。"""

from datetime import UTC, datetime
from time import perf_counter
from typing import Any

import httpx
from pydantic import ValidationError

from app.operations.langfuse_observability import (
    langfuse_observation,
    update_langfuse_observation,
)
from app.search.base import WEB_SEARCH_ERROR_MESSAGES, WebSearchProviderError
from app.search.schemas import (
    EvidenceCoverage,
    SearchResearchContext,
    WebSearchResponse,
    WebSearchResult,
)
from app.security.http_response import HttpResponseBudgetError, async_bounded_request
from app.security.tool_outbound import require_credential_free_payload


class DeepSeekSearchApiClient:
    provider = "deepseek_search_api"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("联网搜索必须配置 API Key。")
        self._api_key = api_key
        self._endpoint = base_url.rstrip("/").removesuffix("/v1") + "/v1/tools/search"
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client

    async def search(
        self,
        *,
        query: str,
        max_results: int,
        research_context: SearchResearchContext | dict | None = None,
    ) -> WebSearchResponse:
        query = query.strip()
        if not 1 <= len(query) <= 128:
            raise ValueError("搜索词必须为 1～128 个字符。")
        if isinstance(max_results, bool) or not 1 <= max_results <= 10:
            raise ValueError("max_results 必须为 1～10。")
        context = (
            SearchResearchContext.model_validate(research_context)
            if research_context is not None
            else None
        )
        payload = {"query": query, "count": max_results, "need_content": False}
        if context and context.original_question:
            payload["ori_question"] = context.original_question
        require_credential_free_payload(payload)
        started = perf_counter()
        with langfuse_observation(
            as_type="span",
            name="deepseek.search.api",
            input=payload,
            metadata={"search_provider": self.provider},
        ) as observation:
            try:
                if self._http_client is not None:
                    data = await self._request(self._http_client, payload)
                else:
                    async with httpx.AsyncClient() as client:
                        data = await self._request(client, payload)
                result, skipped = _normalize(query, data, max_results)
            except WebSearchProviderError as exc:
                update_langfuse_observation(
                    observation,
                    level="ERROR",
                    status_message=exc.code,
                    metadata={
                        "search_provider": self.provider,
                        "search_error_code": exc.code,
                        "search_http_status": exc.status_code,
                        "search_duration_ms": round((perf_counter() - started) * 1000),
                    },
                )
                raise
            update_langfuse_observation(
                observation,
                bounded_content=True,
                output=result.model_dump(mode="json"),
                metadata={
                    "search_provider": self.provider,
                    "search_http_status": 200,
                    "search_result_count": len(result.results),
                    "search_skipped_count": skipped,
                    "search_truncated": any(
                        isinstance(row, dict)
                        and isinstance(row.get("summary"), str)
                        and len(row["summary"]) > 4000
                        for row in data["results"]
                    ),
                    "search_duration_ms": round((perf_counter() - started) * 1000),
                },
            )
            return result

    async def _request(self, client: httpx.AsyncClient, payload: dict) -> Any:
        failure = None
        try:
            response = await async_bounded_request(
                client, "POST", self._endpoint, json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout_seconds=self._timeout_seconds,
            )
            response.raise_for_status()
        except HttpResponseBudgetError as error:
            expired = error.code == "http_response_deadline"
            failure = _error("web_search_timeout" if expired else "web_search_response_limit_exceeded", retryable=expired)
        except httpx.TimeoutException:
            failure = _error("web_search_timeout", retryable=True)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            code = {401:"web_search_auth_failed",402:"web_search_insufficient_balance",
                    408:"web_search_timeout",429:"web_search_rate_limited",504:"web_search_timeout"}.get(status,"web_search_http_error")
            failure = _error(code, retryable=status in {408,429} or status >= 500, status_code=status)
        except httpx.HTTPError:
            failure = _error("web_search_unavailable", retryable=True)
        if failure is not None:
            raise failure
        try:
            data = response.json()
        except (ValueError, RecursionError):
            failure = _error("web_search_response_invalid", status_code=response.status_code)
        if failure is not None:
            raise failure
        return data


def _error(code: str, *, retryable: bool = False, status_code: int | None = None):
    return WebSearchProviderError(
        code=code,
        message=WEB_SEARCH_ERROR_MESSAGES[code],
        retryable=retryable,
        status_code=status_code,
    )


def _published_at(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC).isoformat() if parsed.tzinfo else None
    except ValueError:
        return None


def _normalize(query: str, data: Any, max_results: int) -> tuple[WebSearchResponse, int]:
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise _error("web_search_response_invalid", status_code=200)
    if not data["results"]:
        raise _error("web_search_no_results", status_code=200)
    results, seen, skipped = [], set(), 0
    for row in data["results"]:
        if not isinstance(row, dict):
            skipped += 1
            continue
        summary = row.get("summary")
        text = summary if isinstance(summary, str) and summary.strip() else row.get("snippet")
        title = row.get("title")
        try:
            item = WebSearchResult(
                title=title.strip()[:500] if isinstance(title, str) else "",
                url=row.get("url"),
                snippet=text[:4000] if isinstance(text, str) else "",
                published_at=_published_at(row.get("date")),
            )
        except ValidationError:
            skipped += 1
            continue
        url = str(item.url)
        if url in seen:
            skipped += 1
            continue
        seen.add(url)
        results.append(item)
        if len(results) == max_results:
            break
    if not results:
        raise _error("web_search_response_invalid", status_code=200)
    answer = data.get("answer")
    return WebSearchResponse(
        query=query,
        answer=answer.strip()[:20_000] if isinstance(answer, str) else "",
        results=results,
        coverage=EvidenceCoverage(status="unknown"),
    ), skipped
