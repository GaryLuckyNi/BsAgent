"""搜索客户端的公共契约与固定错误消息。"""

from typing import Protocol

from app.search.schemas import SearchResearchContext, WebSearchResponse

WEB_SEARCH_ERROR_MESSAGES = {
    "web_search_timeout": "联网搜索服务响应超时。",
    "web_search_http_error": "联网搜索服务请求失败。",
    "web_search_unavailable": "联网搜索服务暂不可用。",
    "web_search_response_invalid": "联网搜索服务返回了无效响应。",
    "web_search_response_incomplete": "联网搜索会话未完成。",
    "web_search_no_results": "联网搜索未返回可验证来源。",
    "web_search_auth_failed": "联网搜索服务认证失败，请检查服务配置。",
    "web_search_insufficient_balance": "联网搜索服务余额不足。",
    "web_search_rate_limited": "联网搜索服务请求过于频繁，请稍后重试。",
}


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


class WebSearchClient(Protocol):
    async def search(
        self,
        *,
        query: str,
        max_results: int,
        research_context: SearchResearchContext | dict | None = None,
    ) -> WebSearchResponse: ...
