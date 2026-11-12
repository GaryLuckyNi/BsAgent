"""本轮网页目录：仅展示搜索实际返回的公开网页，不扩大模型证据范围。"""

from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from app.graph.planning.answer_citations import AnswerCitations
from app.security.public_output import public_text, public_web_url


def _url_key(value: str) -> str | None:
    if not public_web_url(value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return None
        return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
    except ValueError:
        return None


def collect_search_sources(state: Mapping, citation_sources=()) -> list[dict]:
    """图状态及回执属于当前 request 的 checkpoint；不读取会话历史或旧消息。"""
    by_url: dict[str, list[dict]] = {}
    highest = 0
    for source in AnswerCitations.previews(citation_sources):
        key = _url_key(source["url"])
        if key:
            by_url.setdefault(key, []).append(source)
            highest = max(highest, source["index"])

    ordered_urls = []
    for record in state.get("tool_calls", []):
        # 结构化数据结果可能很大，先按受信调用名筛选，避免为网页目录序列化其他数据源。
        if hasattr(record, "request") and record.request.tool_name != "web_search":
            continue
        row = record.model_dump(mode="json") if hasattr(record, "model_dump") else record
        if not isinstance(row, dict) or (row.get("request") or {}).get("tool_name") != "web_search":
            continue
        result = row.get("result") or {}
        if result.get("status") != "success" or result.get("tool_name") != "web_search":
            continue
        payload = result.get("result")
        if not isinstance(payload, dict):
            continue
        items = payload.get("results")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            key = _url_key(item["url"])
            if not key:
                continue
            if key not in by_url:
                highest += 1
                by_url[key] = AnswerCitations.previews([{**item, "source_id": f"source_{highest}"}])
            # 种子标题优先，仅用实际搜索返回值补齐缺项，不改写原始回执。
            for source in by_url[key]:
                for field, limit in (("title", 500), ("snippet", 2000), ("published_at", 100)):
                    value = item.get(field)
                    if not source.get(field) and isinstance(value, str) and value.strip():
                        source[field] = public_text(value).strip()[:limit]
            if key not in ordered_urls:
                ordered_urls.append(key)
    ordered_urls.extend(key for key in by_url if key not in ordered_urls)
    return [source for key in ordered_urls for source in by_url[key]]
