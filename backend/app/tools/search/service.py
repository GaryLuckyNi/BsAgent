import re
from collections.abc import Sequence

from app.schemas.tools import ToolSpec
from app.tools.execution_gateway import has_required_scopes, matches_tool_name
from app.tools.registry import ToolRegistry
from app.tools.search.contracts import (
    ToolSearchContext,
    ToolSearchInput,
    ToolSearchResponse,
)
from app.tools.search.index import ToolSearchIndex
from app.tools.source_identity import source_identity


class ToolSearchService:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        index: ToolSearchIndex,
        candidate_k: int,
    ) -> None:
        if registry.catalog_digest != index.catalog_digest:
            raise ValueError("工具目录与搜索索引 digest 不一致。")
        self._registry = registry
        self._index = index
        self._candidate_k = candidate_k

    @property
    def catalog_digest(self) -> str:
        return self._registry.catalog_digest

    def search(
        self,
        request: ToolSearchInput,
        *,
        context: ToolSearchContext,
    ) -> ToolSearchResponse:
        visible_names = {
            spec.name
            for spec in self._registry.specs()
            if spec.name != "search_tool"
            and spec.name not in context.excluded_tools
            and (not context.read_only_only or spec.effect_type == "read_only" or spec.name == "web_search")
            and has_required_scopes(
                granted=context.permission_scopes,
                required=spec.permission_scope,
            )
            and self._allowed_by_skill(
                spec.name,
                skill_allowed_tools=context.skill_allowed_tools,
            )
            and _compatible_description(context.task_goal or request.query, spec.description)
            and _compatible_description(request.query, spec.description)
        }
        return self._index.search(
            request.query,
            top_k=request.top_k,
            candidate_k=self._candidate_k,
            allowed_tool_names=visible_names,
        )

    def resolve_specs(self, tool_names: Sequence[str]) -> list[ToolSpec]:
        return [self._registry.get(tool_name).spec for tool_name in tool_names]

    def execution_binding_digest(self, name: str, version: str) -> str:
        return self._registry.get(name, version).execution_binding_digest

    def source_id(self, name: str) -> str:
        return source_identity(name, self._registry.get(name).execution_binding)

    def search_by_source(self, request, *, context, per_source=3):
        """每个只读 MCP 来源独立召回，避免单一来源占满全局 Top-K。"""
        grouped = {}
        for spec in self._registry.specs():
            if (not spec.name.startswith("mcp.") or spec.effect_type != "read_only"
                    or spec.name in context.excluded_tools
                    or not has_required_scopes(granted=context.permission_scopes, required=spec.permission_scope)
                    or not self._allowed_by_skill(spec.name, skill_allowed_tools=context.skill_allowed_tools)
                    or not _compatible_description(context.task_goal or request.query, spec.description)
                    or not _compatible_description(request.query, spec.description)):
                continue
            grouped.setdefault(self.source_id(spec.name), set()).add(spec.name)
        hits = []
        for names in grouped.values():
            response = self._index.search(request.query, top_k=max(1, min(per_source, 3)),
                candidate_k=self._candidate_k, allowed_tool_names=names)
            hits.extend(response.hits)
        # 分源分数只作召回排序，不作为跨源置信度；最终顺序由模型按目标匹配选择。
        hits.sort(key=lambda hit: (-hit.relevance_score, hit.tool_name))
        retained = [hit.model_copy(update={"rank": index + 1}) for index, hit in enumerate(hits)]
        return ToolSearchResponse(catalog_digest=self.catalog_digest, mode="bm25", hits=retained)

    def tools_from_sources(self, sources: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._registry.specs() if self.source_id(spec.name) in sources)

    @staticmethod
    def _allowed_by_skill(
        tool_name: str,
        *,
        skill_allowed_tools: tuple[str, ...] | None,
    ) -> bool:
        return skill_allowed_tools is None or matches_tool_name(
            tool_name,
            skill_allowed_tools,
        )


def _compatible_description(goal: str, description: str) -> bool:
    """只排除描述明确冲突的市场/时态，不从工具名推测权限或未知能力。"""
    # “不要使用美股”是排除项，不能把出现“美股”一词当作新增市场授权。
    goal = re.sub(r"(?:不得|不要|禁止|不)(?:使用|查询|涉及|含|用)?\s*(?:美股|港股|海外指数|国际主要指数)", "", goal)
    markets = set()
    if re.search(r"A股|沪深|(?:\d{6}\.(?:SH|SZ|BJ))", goal, re.IGNORECASE):
        markets.add("cn")
    if re.search(r"港股|香港股票|\d{5}\.HK", goal, re.IGNORECASE):
        markets.add("hk")
    if re.search(r"美股|美国股票", goal):
        markets.add("us")
    if re.search(r"全球|海外|国际|标普|纳斯达克|道琼斯", goal):
        markets.add("global")
    described = set()
    for market, pattern in (("cn",r"A股|沪深|国内指数"),("hk",r"港股|香港股票"),("us",r"美股|美国股票"),
                            ("global",r"国际主要指数|全球指数|海外指数")):
        if re.search(pattern, description):
            described.add(market)
    if markets and described and not markets.intersection(described):
        return False
    historical = bool(re.search(r"历史|20\d{2}(?:年|[-/])", goal)) and not re.search(r"实时|今日|今天|当前", goal)
    realtime_only = bool(re.search(r"实时(?:日线|分钟|行情)", description)) and "历史" not in description
    return not (historical and realtime_only)
