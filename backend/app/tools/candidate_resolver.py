from dataclasses import dataclass
from typing import Literal

from app.core.config import settings
from app.schemas.tools import ToolSpec
from app.tools.execution_gateway import has_required_scopes, matches_tool_name
from app.tools.runtime_capabilities import (
    CapabilityObservation,
    CapabilityScope,
    matches_observation,
)
from app.tools.search import ToolSearchContext, ToolSearchHit, ToolSearchInput, ToolSearchService

_PLATFORM_TOOL_NAMES = frozenset({"search_tool", "web_search"})
_MIN_LEXICAL_COVERAGE = 0.15


@dataclass(frozen=True)
class ToolCandidateResolution:
    candidates: tuple[ToolSpec, ...]
    hits: tuple[ToolSearchHit, ...]
    tool_choice: Literal["auto", "required"]
    fallback_reason: str | None
    availability_skips: tuple[dict, ...] = ()


class ToolCandidateResolver:
    def __init__(
        self,
        *,
        search_service: ToolSearchService,
        top_k: int | None = None,
    ) -> None:
        self._search_service = search_service
        self._top_k = top_k or settings.tool_search_top_k

    @property
    def catalog_digest(self) -> str:
        return self._search_service.catalog_digest

    def resolve(
        self,
        *,
        task_goal: str,
        tool_search_query: str | None = None,
        tenant_id: str,
        user_id: str,
        permission_scopes: tuple[str, ...],
        has_successful_dependencies: bool,
        web_search_allowed: bool = True,
        skill_allowed_tools: tuple[str, ...] | None = None,
        request_id: str | None = None,
        capability_observations: tuple[CapabilityObservation, ...] = (),
    ) -> ToolCandidateResolution:
        response = self._search_service.search(
            ToolSearchInput(query=tool_search_query or task_goal, top_k=self._top_k),
            context=ToolSearchContext(
                tenant_id=tenant_id,
                user_id=user_id,
                permission_scopes=permission_scopes,
                skill_allowed_tools=skill_allowed_tools,
                excluded_tools=tuple(sorted(_PLATFORM_TOOL_NAMES)),
            ),
        )
        hits = tuple(
            hit
            for hit in response.hits
            if hit.tool_name not in _PLATFORM_TOOL_NAMES and self._qualified(hit)
        )
        specs = (
            list(self._search_service.resolve_specs([hit.tool_name for hit in hits]))
            if hits
            else []
        )
        reason = None if specs else "no_specialized_candidate"
        skipped = []
        if request_id and capability_observations:
            binding = getattr(self._search_service, "execution_binding_digest", None)
            retained = []
            for spec in specs:
                scope = CapabilityScope(tenant_id, user_id, request_id, spec.name, spec.version,
                                        binding(spec.name, spec.version), None) if callable(binding) else None
                observation = next((item for item in capability_observations
                                    if scope is not None and matches_observation(
                                        item, scope, arguments_fingerprint=None)), None)
                if observation is None:
                    retained.append(spec)
                else:
                    skipped.append({"event": "availability_skip", "tool_name": spec.name,
                                    "observation_call_id": observation.original_call_id,
                                    "denied_scope": observation.denied_scope})
            specs = retained
            if skipped and not specs:
                reason = "runtime_capability_unavailable"
        if web_search_allowed:
            try:
                web_spec = self._search_service.resolve_specs(["web_search"])[0]
            except (KeyError, IndexError):
                reason = reason or "web_search_unavailable"
            else:
                permitted = has_required_scopes(
                    granted=permission_scopes, required=web_spec.permission_scope
                )
                scoped = skill_allowed_tools is None or matches_tool_name(
                    "web_search", skill_allowed_tools
                )
                if permitted and scoped:
                    specs.append(web_spec)
                elif not specs:
                    reason = "web_search_permission_denied"
        elif not specs:
            reason = "research_budget_exhausted"
        return ToolCandidateResolution(
            candidates=tuple(specs), hits=hits, tool_choice="auto", fallback_reason=reason,
            availability_skips=tuple(skipped),
        )

    def _qualified(self, hit: ToolSearchHit) -> bool:
        return (
            "exact" in hit.sources
            or ("bm25" in hit.sources and hit.lexical_score >= _MIN_LEXICAL_COVERAGE)
        )
