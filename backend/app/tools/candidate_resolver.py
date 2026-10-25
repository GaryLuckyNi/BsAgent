from dataclasses import dataclass
from typing import Literal

from app.core.config import settings
from app.schemas.tools import ToolSpec
from app.tools.execution_gateway import has_required_scopes, matches_tool_name
from app.tools.planning_candidates import PlanningToolSnapshot, resolve_candidate_refs
from app.tools.registry import ToolCatalogChangedError
from app.tools.runtime_capabilities import (
    CapabilityObservation,
    CapabilityScope,
    matches_observation,
)
from app.tools.search import ToolSearchContext, ToolSearchHit, ToolSearchInput, ToolSearchService
from app.tools.search.service import _compatible_description

_PLATFORM_TOOL_NAMES = frozenset({"search_tool", "web_search"})
_MIN_LEXICAL_COVERAGE = 0.15


@dataclass(frozen=True)
class ToolCandidateResolution:
    candidates: tuple[ToolSpec, ...]
    hits: tuple[ToolSearchHit, ...]
    tool_choice: Literal["auto", "required"]
    fallback_reason: str | None
    availability_skips: tuple[dict, ...] = ()
    source: Literal["task_search", "planning_reuse", "skill_named_tool"] = "task_search"


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
        planning_snapshot: PlanningToolSnapshot | None = None,
        candidate_refs: tuple[str, ...] = (),
    ) -> ToolCandidateResolution:
        def permitted(spec):
            return has_required_scopes(granted=permission_scopes, required=spec.permission_scope) and (
                skill_allowed_tools is None or matches_tool_name(spec.name, skill_allowed_tools))

        hits, specs, reason, source = (), [], None, "task_search"
        needs_search = not candidate_refs
        if candidate_refs:
            cards = resolve_candidate_refs(planning_snapshot, candidate_refs,
                request_id=request_id, catalog_digest=self.catalog_digest)
            hits = tuple(ToolSearchHit(tool_name=card.tool_name, tool_version=card.tool_version,
                description=card.description, rank=index + 1, sources=card.sources)
                for index, card in enumerate(cards))
            try:
                specs = self._search_service.resolve_specs([card.tool_name for card in cards])
            except KeyError as exc:
                raise ToolCatalogChangedError() from exc
            if len(specs) != len(cards) or any(
                (card.tool_name, card.tool_version) != (spec.name, spec.version)
                for card, spec in zip(cards, specs, strict=True)
            ):
                raise ToolCatalogChangedError()
            source = "planning_reuse"
            specs = [spec for spec in specs if permitted(spec)]
            if not specs:
                reason = "tool_permission_denied"
            elif not any(_compatible_description(task_goal, spec.description) for spec in specs):
                needs_search = True
                reason = "planning_candidates_incompatible"
        if needs_search:
            response = self._search_service.search(
                ToolSearchInput(query=tool_search_query or task_goal, top_k=self._top_k),
                context=ToolSearchContext(
                    tenant_id=tenant_id, user_id=user_id, permission_scopes=permission_scopes,
                    skill_allowed_tools=skill_allowed_tools,
                    excluded_tools=tuple(sorted(_PLATFORM_TOOL_NAMES)), task_goal=task_goal,
                ),
            )
            hits = tuple(hit for hit in response.hits
                         if hit.tool_name not in _PLATFORM_TOOL_NAMES and self._qualified(hit))
            specs = list(self._search_service.resolve_specs([hit.tool_name for hit in hits])) if hits else []
            source = "task_search"
        specs = [spec for spec in specs if spec.name not in _PLATFORM_TOOL_NAMES
                 and permitted(spec) and _compatible_description(task_goal, spec.description)]
        reason = reason or (None if specs else "no_specialized_candidate")
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
                web_permitted = has_required_scopes(
                    granted=permission_scopes, required=web_spec.permission_scope
                )
                scoped = skill_allowed_tools is None or matches_tool_name(
                    "web_search", skill_allowed_tools
                )
                if web_permitted and scoped:
                    specs.append(web_spec)
                elif not specs:
                    reason = "web_search_permission_denied"
        elif not specs and reason not in {"tool_permission_denied", "planning_candidates_incompatible", "runtime_capability_unavailable"}:
            reason = "research_budget_exhausted"
        return ToolCandidateResolution(
            candidates=tuple(specs), hits=hits, tool_choice="auto", fallback_reason=reason,
            availability_skips=tuple(skipped),
            source=source,
        )

    def _qualified(self, hit: ToolSearchHit) -> bool:
        return (
            "exact" in hit.sources
            or ("bm25" in hit.sources and hit.lexical_score >= _MIN_LEXICAL_COVERAGE)
        )
