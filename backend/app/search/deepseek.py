from typing import Any

import httpx

from app.search.schemas import WebSearchResponse, WebSearchResult


class WebSearchProviderError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(message)


class DeepSeekWebSearchClient:
    provider = "deepseek"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DeepSeek api key is required for web search.")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client

    async def search(self, *, query: str, max_results: int) -> WebSearchResponse:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Web search query must not be empty.")
        if not 1 <= max_results <= 10:
            raise ValueError("max_results must be between 1 and 10.")
        payload = {
            "model": self._model,
            "max_tokens": 768,
            "thinking": {"type": "disabled"},
            "system": (
                "你是投研实体发现搜索助手。只能输出一个 JSON object，不要输出 Markdown 或解释。"
                "只依据搜索来源判断用户公司名与 A 股上市主体的关系，"
                "不得将同行、竞争对手、供应商、客户或概念股当作同一实体。"
                "必须输出 status、candidate_query、canonical_name、symbol、"
                "relation_type、confidence、source_urls、reason。"
                "candidate_query 优先使用可供市场数据源复验的证券简称；"
                "source_urls 只能使用本次搜索实际引用的 URL。"
                "source_urls 最多 2 个，reason 不超过 40 个中文字符。"
                "relation_type 只能是 same_entity、alias、legal_name、listed_subject、"
                "security_code、subsidiary_of_listed_subject、peer_company、"
                "related_company、supplier_customer 或 unknown。"
                "证据不能唯一确认时，status 必须为 unresolved。"
            ),
            "messages": [{"role": "user", "content": normalized_query}],
            "tools": [
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 1,
                }
            ],
        }
        if self._http_client is not None:
            turns = await self._run_search_turns(
                self._http_client,
                payload=payload,
                query=normalized_query,
            )
        else:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            ) as client:
                turns = await self._run_search_turns(
                    client,
                    payload=payload,
                    query=normalized_query,
                )
        return _normalize_response(
            query=normalized_query,
            payload=_merge_turn_payloads(turns),
            max_results=max_results,
        )

    async def _run_search_turns(
        self,
        client: httpx.AsyncClient,
        *,
        payload: dict[str, Any],
        query: str,
    ) -> list[dict[str, Any]]:
        first_turn = await self._request(client, payload)
        if not _needs_search_continuation(first_turn):
            return [first_turn]

        content = first_turn.get("content")
        if not isinstance(content, list):
            raise _incomplete_search_response()
        continuation_payload = {
            **payload,
            "max_tokens": 4_096,
            "output_config": {"effort": "low"},
            "messages": [
                {"role": "user", "content": query},
                {"role": "assistant", "content": content},
            ],
        }
        continuation = await self._request(client, continuation_payload)
        if _needs_search_continuation(continuation) or not _has_text_content(
            continuation
        ):
            raise _incomplete_search_response()
        return [first_turn, continuation]

    async def _request(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = await client.post(
                "/anthropic/v1/messages",
                json=payload,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise WebSearchProviderError(
                code="web_search_timeout",
                message="联网搜索服务响应超时。",
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
            raise WebSearchProviderError(
                code="web_search_http_error",
                message="联网搜索服务请求失败。",
                retryable=retryable,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise WebSearchProviderError(
                code="web_search_unavailable",
                message="联网搜索服务暂不可用。",
                retryable=True,
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise WebSearchProviderError(
                code="web_search_response_invalid",
                message="联网搜索服务返回了无效响应。",
                retryable=False,
            ) from exc
        if not isinstance(data, dict):
            raise WebSearchProviderError(
                code="web_search_response_invalid",
                message="联网搜索服务返回了无效响应。",
                retryable=False,
            )
        return data


def _needs_search_continuation(payload: dict[str, Any]) -> bool:
    if payload.get("stop_reason") == "pause_turn":
        return True
    content = payload.get("content")
    if not isinstance(content, list) or _has_text_content(payload):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "web_search_tool_result"
        for block in content
    )


def _has_text_content(payload: dict[str, Any]) -> bool:
    content = payload.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and bool(block["text"].strip())
        for block in content
    )


def _merge_turn_payloads(turns: list[dict[str, Any]]) -> dict[str, Any]:
    content: list[Any] = []
    for turn in turns:
        turn_content = turn.get("content")
        if isinstance(turn_content, list):
            content.extend(turn_content)
    usage = turns[-1].get("usage", {}) if turns else {}
    return {
        "content": content,
        "usage": usage if isinstance(usage, dict) else {},
    }


def _incomplete_search_response() -> WebSearchProviderError:
    return WebSearchProviderError(
        code="web_search_response_incomplete",
        message="联网搜索会话未完成。",
        retryable=True,
    )


def _normalize_response(
    *,
    query: str,
    payload: dict[str, Any],
    max_results: int,
) -> WebSearchResponse:
    content = payload.get("content")
    if not isinstance(content, list):
        raise WebSearchProviderError(
            code="web_search_response_invalid",
            message="联网搜索服务返回了无效响应。",
            retryable=False,
        )

    answer_parts: list[str] = []
    raw_results: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            answer_parts.append(block["text"].strip())
            citations = block.get("citations")
            if isinstance(citations, list):
                raw_results.extend(item for item in citations if isinstance(item, dict))
        if block_type == "web_search_tool_result":
            result_content = block.get("content")
            if isinstance(result_content, list):
                raw_results.extend(
                    item for item in result_content if isinstance(item, dict)
                )

    results: list[WebSearchResult] = []
    seen_urls: set[str] = set()
    for item in raw_results:
        raw_url = item.get("url")
        raw_title = item.get("title")
        if not isinstance(raw_url, str) or not isinstance(raw_title, str):
            continue
        url = raw_url.strip()
        title = raw_title.strip()
        if not url or not title or url in seen_urls:
            continue
        seen_urls.add(url)
        snippet = item.get("snippet") or item.get("content") or ""
        if not isinstance(snippet, str):
            snippet = ""
        published_at = item.get("page_age") or item.get("published_at")
        if published_at is not None and not isinstance(published_at, str):
            published_at = None
        results.append(
            WebSearchResult(
                title=title[:500],
                url=url,
                snippet=snippet[:4_000],
                published_at=published_at[:100] if published_at else None,
            )
        )
        if len(results) >= max_results:
            break

    if not results:
        raise WebSearchProviderError(
            code="web_search_no_results",
            message="联网搜索未返回可验证来源。",
            retryable=False,
        )
    usage = payload.get("usage")
    return WebSearchResponse(
        query=query,
        answer="\n".join(part for part in answer_parts if part),
        results=results,
        usage=usage if isinstance(usage, dict) else {},
    )
