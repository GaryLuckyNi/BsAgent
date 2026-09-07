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
            and has_required_scopes(
                granted=context.permission_scopes,
                required=spec.permission_scope,
            )
            and self._allowed_by_skill(
                spec.name,
                skill_allowed_tools=context.skill_allowed_tools,
            )
        }
        return self._index.search(
            request.query,
            top_k=request.top_k,
            candidate_k=self._candidate_k,
            allowed_tool_names=visible_names,
        )

    def resolve_specs(self, tool_names: Sequence[str]) -> list[ToolSpec]:
        return [self._registry.get(tool_name).spec for tool_name in tool_names]

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
