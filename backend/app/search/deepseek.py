import json
from typing import Any

import httpx

from app.operations.langfuse_observability import langfuse_generation, update_langfuse_observation
from app.search.base import WEB_SEARCH_ERROR_MESSAGES, WebSearchProviderError
from app.search.schemas import (
    EvidenceCoverage,
    SearchResearchContext,
    WebSearchResponse,
    WebSearchResult,
)
from app.security.http_response import HttpResponseBudgetError, async_bounded_request
from app.security.tool_outbound import require_credential_free_payload


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

    async def search(
        self, *, query: str, max_results: int,
        research_context: SearchResearchContext | dict | None = None,
    ) -> WebSearchResponse:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Web search query must not be empty.")
        if not 1 <= max_results <= 10:
            raise ValueError("max_results must be between 1 and 10.")
        if research_context is not None:
            research_context = SearchResearchContext.model_validate(research_context)
        payload = {
            "model": self._model,
            "max_tokens": 768,
            "thinking": {"type": "disabled"},
            "system": (
                "你是投研联网检索助手。根据用户 query 自主检索可验证的公开来源，"
                "覆盖当日市场涨跌原因、政策、新闻、公告、行业动态、事实核验或实体关系。"
                "回答必须只依据本次搜索结果，使用简洁中文概括关键事实和不确定性，"
                "不得补造数字、时间、因果关系或来源。"
                "当 query 要求解析公司、上市主体或证券代码时，必须明确说明实体关系，"
                "不得把同行、竞争对手、供应商、客户或概念股当作同一实体；"
                "无法唯一确认时必须明确表示未确认。"
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
        if research_context is not None:
            payload["system"] += (
                "research_context 是服务端绑定的本轮研究目标，query 只是检索建议；必须围绕该目标取证，"
                "不得改查全局问题。以 as_of 为信息截止时点，不把此后才会产生的数据当作当前应已取得的证据。"
                "没有取得的指标必须说明缺失，不得用相关行情或原因代替估值等指定事实。"
                "输出 JSON 对象：answer 为简洁事实与限制，covered_criteria 为已覆盖验收条件的从0开始的索引数组；"
                "不能核实的条件不要列入，不输出没有依据的覆盖声明。"
            )
            payload["messages"] = [{"role": "user", "content": json.dumps({
                "query": normalized_query, "research_context": research_context.model_dump(mode="json"),
            }, ensure_ascii=False)}]
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
        response = _normalize_response(
            query=normalized_query,
            payload=_merge_turn_payloads(turns),
            max_results=max_results,
        )
        return _with_coverage(response, research_context) if research_context else response

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
                *payload["messages"],
                {"role": "assistant", "content": content},
            ],
        }
        continuation = await self._request(client, continuation_payload)
        if _needs_search_continuation(continuation) or not _has_text_content(continuation):
            raise _incomplete_search_response()
        return [first_turn, continuation]

    async def _request(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        require_credential_free_payload(payload)
        with langfuse_generation(
            name="deepseek.web_search.turn", model=self._model,
            model_parameters={"max_tokens": payload.get("max_tokens"), "thinking": False},
        ) as generation:
            update_langfuse_observation(
                generation, bounded_content=True,
                input={"system_prompt": payload.get("system"),
                       "messages": _trace_messages(payload.get("messages"))},
            )
            try:
                data = await self._request_json(client, payload)
            except WebSearchProviderError:
                update_langfuse_observation(generation, level="ERROR", status_message="llm_http_error")
                raise
            content = data.get("content")
            blocks = content if isinstance(content, list) else []
            usage = data.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            usage_details = {
                target: usage[source] for source, target in (
                    ("input_tokens", "input"), ("output_tokens", "output"),
                    ("cache_read_input_tokens", "input_cache_read"),
                    ("cache_creation_input_tokens", "input_cache_creation"),
                )
                if isinstance(usage.get(source), int) and not isinstance(usage[source], bool)
            }
            update_langfuse_observation(
                generation, bounded_content=True,
                output={
                    "stop_reason": data.get("stop_reason"),
                    "answer": "\n".join(block["text"] for block in blocks
                                        if isinstance(block, dict) and block.get("type") == "text"
                                        and isinstance(block.get("text"), str)),
                    "tool_calls": [
                        {"name": block.get("name"), "input": block.get("input")}
                        for block in blocks if isinstance(block, dict)
                        and block.get("type") == "server_tool_use"
                    ],
                },
                usage_details=usage_details,
                metadata={"finish_reason": data.get("stop_reason")},
            )
            return data

    async def _request_json(
        self, client: httpx.AsyncClient, payload: dict[str, Any],
    ) -> dict[str, Any]:
        failure = None
        try:
            response = await async_bounded_request(
                client, "POST", "/anthropic/v1/messages",
                budget_kind="model",
                timeout_seconds=self._timeout_seconds,
                json=payload,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            response.raise_for_status()
        except HttpResponseBudgetError as error:
            expired = error.code == "http_response_deadline"
            code = "web_search_timeout" if expired else "web_search_response_limit_exceeded"
            failure = WebSearchProviderError(code=code, message=WEB_SEARCH_ERROR_MESSAGES[code], retryable=expired)
        except httpx.TimeoutException:
            failure = WebSearchProviderError(
                code="web_search_timeout",
                message="联网搜索服务响应超时。",
                retryable=True,
            )
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
            failure = WebSearchProviderError(
                code="web_search_http_error",
                message="联网搜索服务请求失败。",
                retryable=retryable,
                status_code=exc.response.status_code,
            )
        except httpx.HTTPError:
            failure = WebSearchProviderError(
                code="web_search_unavailable",
                message="联网搜索服务暂不可用。",
                retryable=True,
            )
        if failure is not None:
            raise failure
        try:
            data = response.json()
        except (ValueError, RecursionError):
            failure = WebSearchProviderError(
                code="web_search_response_invalid",
                message="联网搜索服务返回了无效响应。",
                retryable=False,
            )
        if failure is not None:
            raise failure
        if not isinstance(data, dict):
            raise WebSearchProviderError(
                code="web_search_response_invalid",
                message="联网搜索服务返回了无效响应。",
                retryable=False,
            )
        return data


def _trace_messages(messages: Any) -> list[dict[str, Any]]:
    """只投影已知的可读字段，不把服务端续轮的 opaque 内容写入观测。"""
    projected = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            blocks = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    blocks.append({"type": kind, "text": block.get("text")})
                elif kind == "server_tool_use":
                    blocks.append({key: block[key] for key in ("type", "id", "name", "input")
                                   if key in block})
                elif kind == "web_search_tool_result":
                    sources = block.get("content")
                    blocks.append({
                        "type": kind, "tool_use_id": block.get("tool_use_id"),
                        "content": [
                            {key: source[key] for key in
                             ("type", "title", "url", "snippet", "page_age", "published_at")
                             if key in source}
                            for source in sources if isinstance(source, dict)
                        ] if isinstance(sources, list) else [],
                    })
            content = blocks
        elif not isinstance(content, str):
            content = None
        projected.append({"role": message.get("role"), "content": content})
    return projected


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


def _with_coverage(
    response: WebSearchResponse, context: SearchResearchContext,
) -> WebSearchResponse:
    coverage = EvidenceCoverage(status="unknown")
    answer = response.answer
    try:
        payload = json.loads(answer)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("answer"), str):
        answer = payload["answer"]
        covered = payload.get("covered_criteria")
        criteria = context.completion_criteria
        if criteria and isinstance(covered, list) and all(
            isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(criteria)
            for index in covered
        ):
            indexes = set(covered)
            coverage = EvidenceCoverage(
                status="complete" if len(indexes) == len(criteria) else "partial" if indexes else "missing",
                covered_criteria=[text for index, text in enumerate(criteria) if index in indexes],
                missing_criteria=[text for index, text in enumerate(criteria) if index not in indexes],
                basis="search_model",
            )
    return response.model_copy(update={"answer": answer, "coverage": coverage})


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
    usage: dict[str, Any] = {}
    for turn in turns:
        turn_content = turn.get("content")
        if isinstance(turn_content, list):
            content.extend(turn_content)
        _add_usage(usage, turn.get("usage"))
    return {
        "content": content,
        "usage": usage if isinstance(usage, dict) else {},
    }


def _add_usage(total: dict[str, Any], incoming: Any) -> None:
    if not isinstance(incoming, dict):
        return
    for key, value in incoming.items():
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
        elif isinstance(value, dict):
            nested = total.setdefault(key, {})
            if isinstance(nested, dict):
                _add_usage(nested, value)


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
                raw_results.extend(item for item in result_content if isinstance(item, dict))

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
