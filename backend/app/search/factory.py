from app.core.config import settings
from app.search.deepseek import DeepSeekWebSearchClient
from app.tools.web_search import WebSearchClient


def build_web_search_client() -> WebSearchClient:
    if settings.web_search_provider == "deepseek":
        return DeepSeekWebSearchClient(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.web_search_timeout_seconds,
        )
    raise ValueError(f"Unsupported web search provider: {settings.web_search_provider}")
