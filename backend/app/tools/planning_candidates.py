"""规划只读能力卡片；执行仍从同一目录快照加载完整 schema。"""

import json
from hashlib import sha256
from itertools import zip_longest

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.operations.langfuse_content import LangfuseContentPolicy
from app.schemas.tools import ToolSpec
from app.tool_documentation.metadata import effective_description, model_schema, output_field_hints
from app.tools.input_schema import planning_input_schema
from app.tools.registry import ToolCatalogChangedError
from app.tools.search import ToolSearchContext, ToolSearchInput, ToolSearchService
from app.tools.search.contracts import SearchSource
from app.tools.search.documents import tokenize_tool_text
from app.tools.search.index import ToolSearchUnavailableError

MAX_CARD_CHARS = 1200
MAX_CARDS_CHARS = 8000
MAX_INPUT_SCHEMA_CHARS = 4000
MAX_INPUT_SCHEMAS_CHARS = 12000
MAX_SUPPLEMENTED_CANDIDATES = 8


class PlanningToolCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ref: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    tool_version: str = Field(min_length=1)
    description: str
    schema_excerpt: str
    truncated: bool = False
    input_schema: dict | None = None
    sources: list[SearchSource] = Field(default_factory=list, max_length=2)
    source_id: str | None = None

    @model_validator(mode="after")
    def validate_identity_and_budget(self):
        if self.ref != f"tool:{self.tool_name}@{self.tool_version}":
            raise ValueError("候选引用与工具身份不一致。")
        if len(self.description) + len(self.schema_excerpt) > MAX_CARD_CHARS:
            raise ValueError("能力卡片超过字符预算。")
        if self.input_schema is not None and _schema_size(self.input_schema) > MAX_INPUT_SCHEMA_CHARS:
            raise ValueError("完整输入契约超过字符预算。")
        return self


class PlanningToolSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    catalog_digest: str
    query_fingerprint: str
    candidates: list[PlanningToolCard] = Field(default_factory=list, max_length=MAX_SUPPLEMENTED_CANDIDATES)
    truncated: bool = False
    reason: str | None = None

    @model_validator(mode="after")
    def validate_candidates(self):
        if len({card.ref for card in self.candidates}) != len(self.candidates):
            raise ValueError("候选引用不能重复。")
        if sum(len(card.description) + len(card.schema_excerpt) for card in self.candidates) > MAX_CARDS_CHARS:
            raise ValueError("能力卡片总预算超限。")
        if sum(_schema_size(card.input_schema) for card in self.candidates) > MAX_INPUT_SCHEMAS_CHARS:
            raise ValueError("完整输入契约总预算超限。")
        return self


def _clean(text: str) -> str:
    return LangfuseContentPolicy(max_chars=max(1, len(text))).sanitize_text(text)


def _schema_size(schema: dict | None) -> int:
    return len(json.dumps(schema, ensure_ascii=False)) if schema is not None else 0


def project_card(spec: ToolSpec, *, max_chars: int = MAX_CARD_CHARS,
                 max_schema_chars: int = MAX_INPUT_SCHEMA_CHARS, query: str = "") -> PlanningToolCard:
    max_chars = max(1, min(max_chars, MAX_CARD_CHARS))
    required = set(spec.input_schema.get("required", []))
    properties = model_schema(spec).get("properties", {})
    order = sorted(properties, key=lambda name: (name != "fields", name not in required, name))
    lines = []
    for name in order:
        if name in spec.server_injected_fields:
            continue
        item = properties[name]
        if isinstance(item, dict):
            lines.append(f"{name}{'（必需）' if name in required else ''}: {item.get('description', '')}")
    query_tokens = set(tokenize_tool_text(query))
    outputs = sorted(output_field_hints(spec), key=lambda hint: -len(
        query_tokens.intersection(tokenize_tool_text(hint))))
    output_excerpt = _clean("\n".join(f"返回 {hint}" for hint in outputs))
    description, excerpt = _clean(effective_description(spec)), _clean("\n".join(lines))
    description_budget = min(len(description), max_chars // 2) if excerpt or output_excerpt else max_chars
    kept_description = description[:description_budget]
    remaining = max_chars - len(kept_description)
    output_budget = remaining // 2 if excerpt else remaining
    kept_output = output_excerpt[:output_budget]
    projected_excerpt = kept_output + ("\n" if kept_output and excerpt else "") + excerpt
    kept_excerpt = projected_excerpt[:remaining]
    input_schema = planning_input_schema(spec) if spec.risk_level in {"low", "medium"} else None
    if input_schema is not None:
        serialized = json.dumps(input_schema, ensure_ascii=False)
        if len(serialized) > min(max_schema_chars, MAX_INPUT_SCHEMA_CHARS) or _clean(serialized) != serialized:
            input_schema = None  # 不裁剪或改写会用于生成参数的契约。
    return PlanningToolCard(
        ref=f"tool:{spec.name}@{spec.version}", tool_name=spec.name, tool_version=spec.version,
        description=kept_description, schema_excerpt=kept_excerpt,
        truncated=(len(kept_description) < len(description) or len(kept_output) < len(output_excerpt)
                   or len(kept_excerpt) < len(projected_excerpt)),
        input_schema=input_schema,
    )


def recall_candidates(*, state: dict, search_service: ToolSearchService | None,
                      top_k: int, supplemental_queries: tuple[str, ...] = ()) -> PlanningToolSnapshot:
    original = " ".join(state["user_message"].split())
    goal = " ".join(((state.get("intent") or {}).get("goal") or original).split())
    queries = [original[:512]]
    if (len(original) > 512 or len(original) < 12) and goal[:512] not in queries:
        queries.append(goal[:512])
    if supplemental_queries:
        queries = list(dict.fromkeys([*supplemental_queries, *queries]))
        top_k = 8
    top_k = max(1, min(top_k, 8))
    card_limit = MAX_SUPPLEMENTED_CANDIDATES if supplemental_queries else top_k
    digest = search_service.catalog_digest if search_service is not None else state.get("tool_catalog_digest", "")
    if search_service is not None and state.get("tool_catalog_digest") not in (None, "", digest):
        raise ToolCatalogChangedError()
    fingerprint = sha256(json.dumps({
        "queries": queries, "goal": goal[:500], "top_k": top_k, "card_limit": card_limit,
        "request_id": state["request_id"], "tenant_id": state["tenant_id"],
        "user_id": state["user_id"], "scopes": sorted(state["permission_scopes"]),
        "catalog_digest": digest, "truncated": len(original) > 512 or len(goal) > 500,
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    previous = state.get("planning_tool_candidates")
    if previous is not None:
        previous = PlanningToolSnapshot.model_validate(previous)
        if previous.query_fingerprint == fingerprint and previous.catalog_digest == digest:
            return previous
    base = {"request_id": state["request_id"], "catalog_digest": digest,
            "query_fingerprint": fingerprint, "truncated": len(original) > 512 or len(goal) > 500}
    if search_service is None:
        return PlanningToolSnapshot(**base, reason="tool_search_service_missing")
    cards = ({card.tool_name: card for card in previous.candidates}
             if supplemental_queries and previous is not None
             and previous.request_id == state["request_id"] and previous.catalog_digest == digest else {})
    try:
        recalled = []
        for query in queries:
            response = search_service.search(ToolSearchInput(query=query, top_k=top_k), context=ToolSearchContext(
                tenant_id=state["tenant_id"], user_id=state["user_id"],
                permission_scopes=tuple(state["permission_scopes"]),
                excluded_tools=("search_tool", "web_search"), task_goal=goal[:500],
            ))
            if response.catalog_digest != digest:
                raise ToolCatalogChangedError()
            hits = [hit for hit in response.hits
                    if hit.tool_name not in ("search_tool", "web_search") and (
                        "exact" in hit.sources or ("bm25" in hit.sources and hit.lexical_score >= 0.15))]
            specs = search_service.resolve_specs([hit.tool_name for hit in hits])
            recalled.append([(hit, spec, query) for hit, spec in zip(hits, specs, strict=True)])
        rows = ([row for group in zip_longest(*recalled) for row in group if row is not None]
                if supplemental_queries else [row for group in recalled for row in group])
        for hit, spec, query in rows:
            if hit.tool_version != spec.version:
                raise ToolCatalogChangedError()
            if spec.name not in cards and len(cards) < card_limit:
                remaining = MAX_INPUT_SCHEMAS_CHARS - sum(
                    _schema_size(card.input_schema) for card in cards.values())
                remaining_chars = MAX_CARDS_CHARS - sum(
                    len(card.description) + len(card.schema_excerpt) for card in cards.values())
                card = project_card(spec, max_chars=min(MAX_CARD_CHARS, remaining_chars // (card_limit - len(cards))),
                                    max_schema_chars=remaining, query=query)
                cards[spec.name] = card.model_copy(update={"sources": list(hit.sources),
                                                         "source_id": search_service.source_id(spec.name)})
    except ToolSearchUnavailableError:
        fallback = (previous.candidates if supplemental_queries and previous is not None
                    and previous.request_id == state["request_id"] and previous.catalog_digest == digest else [])
        return PlanningToolSnapshot(**base, candidates=list(cards.values()) or fallback, reason="tool_search_unavailable")
    base["truncated"] |= any(card.truncated for card in cards.values())
    return PlanningToolSnapshot(**base, candidates=list(cards.values()),
                                reason=None if cards else "no_specialized_candidate")


def resolve_candidate_refs(snapshot: PlanningToolSnapshot | None, refs: tuple[str, ...], *,
                           request_id: str | None, catalog_digest: str) -> tuple[PlanningToolCard, ...]:
    if snapshot is None or snapshot.request_id != request_id:
        raise ValueError("工具候选未绑定当前请求。")
    if snapshot.catalog_digest != catalog_digest:
        raise ToolCatalogChangedError()
    cards = {card.ref: card for card in snapshot.candidates}
    if len(refs) != len(set(refs)) or any(ref not in cards for ref in refs):
        raise ValueError("工具候选引用未披露或重复。")
    return tuple(cards[ref] for ref in refs)
