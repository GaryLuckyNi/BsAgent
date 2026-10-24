"""模型输入的确定性投影；原始执行结果和审计材料始终不变。"""

import json
from copy import deepcopy
from typing import Any

from app.search.schemas import EvidenceCoverage


def recovery_coverage(value: Any) -> dict[str, Any]:
    """搜索覆盖是自述，不是原始结构化数据或独立验证的证明。"""
    try:
        coverage = EvidenceCoverage.model_validate(value)
    except ValueError:
        coverage = EvidenceCoverage(status="unknown")
    result = coverage.model_dump(mode="json")
    if coverage.status != "unknown":
        result["basis"] = "search_model"
    return bounded_search_coverage(result)


def bounded_search_coverage(value: Any) -> dict[str, Any] | None:
    """仅缩短超长观测副本，保证任务正文仍保留覆盖状态和明确截断标记。"""
    if not isinstance(value, dict):
        return None
    if len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) <= 2000:
        return deepcopy(value)
    result = {key: value[key] for key in ("status", "basis") if key in value}
    result.update(covered_criteria=[], missing_criteria=[], truncated=True)
    for field in ("covered_criteria", "missing_criteria"):
        items = value.get(field)
        for item in items[:8] if isinstance(items, list) else []:
            if not isinstance(item, str):
                continue
            result[field].append(item[:160])
            if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > 2000:
                result[field].pop()
    return result


class EvidenceProjection:
    def __init__(self, *, include_query: bool = False):
        self.include_query = include_query
        self.sources: list[dict[str, Any]] = []
        self._by_url: dict[str, dict[str, Any]] = {}

    def content(self, value: Any) -> Any:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                return value
            if not isinstance(parsed, (dict, list)):
                return value
            value = parsed
        result = deepcopy(value)
        if not isinstance(result, dict):
            return result
        if isinstance(result.get("search_result"), dict):
            result["search_result"] = self.content(result["search_result"])
        if not isinstance(result.get("results"), list):
            return result
        sources = result["results"]
        if not sources and not self.include_query and isinstance(result.get("answer"), str):
            result.pop("query", None)
        if not sources or not all(
            isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and isinstance(item.get("title"), str)
            and set(item) <= {"url", "title", "snippet", "published_at"}
            for item in sources
        ):
            return result
        references = []
        for item in sources:
            url = item["url"]
            source = self._by_url.get(url)
            if source is None:
                source = {
                    "source_id": f"source_{len(self.sources) + 1}",
                    "title": item["title"],
                    "url": url,
                }
                self.sources.append(source)
                self._by_url[url] = source
            for key in ("title", "snippet", "published_at"):
                value = item.get(key)
                if not value:
                    continue
                if key not in source:
                    source[key] = value
                elif source[key] != value:
                    variants = source.setdefault(f"{key}_variants", [source[key]])
                    if value not in variants:
                        variants.append(value)
            if source["source_id"] not in references:
                references.append(source["source_id"])
        result.pop("results")
        result["source_ids"] = references
        if not self.include_query and "answer" in result:
            result.pop("query", None)
        return result
