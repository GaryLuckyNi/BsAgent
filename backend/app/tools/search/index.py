from collections.abc import Sequence
from dataclasses import dataclass

import bm25s

from app.schemas.tools import ToolSpec
from app.tools.search.contracts import (
    SearchMode,
    SearchSource,
    ToolSearchHit,
    ToolSearchResponse,
)
from app.tools.search.documents import (
    ToolSearchDocument,
    build_tool_search_document,
    tokenize_tool_text,
)


class ToolSearchUnavailableError(RuntimeError):
    code = "tool_search_unavailable"

    def __init__(self) -> None:
        super().__init__("工具检索暂不可用。")


@dataclass(frozen=True)
class ToolSearchIndex:
    catalog_digest: str
    _documents: tuple[ToolSearchDocument, ...]
    _bm25: bm25s.BM25 | None

    @classmethod
    def build(
        cls,
        *,
        specs: Sequence[ToolSpec],
        catalog_digest: str,
    ) -> "ToolSearchIndex":
        documents = tuple(build_tool_search_document(spec) for spec in specs)
        retriever = None
        if documents:
            retriever = bm25s.BM25()
            retriever.index(
                [list(document.tokens) for document in documents],
                show_progress=False,
            )
        return cls(
            catalog_digest=catalog_digest,
            _documents=documents,
            _bm25=retriever,
        )

    def search(
        self,
        query: str,
        *,
        top_k: int,
        candidate_k: int,
        allowed_tool_names: set[str] | frozenset[str] | None = None,
    ) -> ToolSearchResponse:
        if not self._documents:
            return ToolSearchResponse(catalog_digest=self.catalog_digest, mode="bm25")

        allowed_ids = self._allowed_document_ids(allowed_tool_names)
        exact_ids = self._exact_matches(query, allowed_ids=allowed_ids)
        if exact_ids:
            return self._response(
                rows=[
                    (document_id, None, 1.0, 0.0, 1.0)
                    for document_id in exact_ids[:top_k]
                ],
                source="exact",
                mode="exact",
            )

        try:
            ranking = self._bm25_ranking(
                query,
                allowed_ids=allowed_ids,
                candidate_k=candidate_k,
            )
        except Exception as exc:
            raise ToolSearchUnavailableError() from exc
        return self._response(
            rows=self._rerank(query, ranking=ranking)[:top_k],
            source="bm25",
            mode="bm25",
        )

    def _rerank(
        self,
        query: str,
        *,
        ranking: Sequence[tuple[int, float]],
    ) -> list[tuple[int, float, float, float, float]]:
        if not ranking:
            return []

        query_tokens = _meaningful_tokens(tokenize_tool_text(query))
        max_bm25 = max(score for _, score in ranking)
        rows = []
        for document_id, bm25_score in ranking:
            document = self._documents[document_id]
            lexical_score = _token_coverage(query_tokens, document.tokens)
            schema_score = _token_coverage(query_tokens, document.schema_tokens)
            bm25_component = bm25_score / max_bm25 if max_bm25 else 0.0
            relevance_score = min(
                1.0,
                0.65 * bm25_component
                + 0.25 * lexical_score
                + 0.10 * schema_score,
            )
            rows.append(
                (
                    document_id,
                    bm25_score,
                    lexical_score,
                    schema_score,
                    relevance_score,
                )
            )
        return sorted(rows, key=lambda item: (-item[4], item[0]))

    def _allowed_document_ids(
        self,
        allowed_tool_names: set[str] | frozenset[str] | None,
    ) -> frozenset[int]:
        if allowed_tool_names is None:
            return frozenset(range(len(self._documents)))
        return frozenset(
            index
            for index, document in enumerate(self._documents)
            if document.tool_name in allowed_tool_names
        )

    def _exact_matches(self, query: str, *, allowed_ids: frozenset[int]) -> list[int]:
        normalized_query = " ".join(query.lower().split())
        return [
            index
            for index, document in enumerate(self._documents)
            if index in allowed_ids and document.tool_name.lower() in normalized_query
        ]

    def _bm25_ranking(
        self,
        query: str,
        *,
        allowed_ids: frozenset[int],
        candidate_k: int,
    ) -> list[tuple[int, float]]:
        if self._bm25 is None or not allowed_ids:
            return []
        results = self._bm25.retrieve(
            [list(tokenize_tool_text(query))],
            k=len(self._documents),
            show_progress=False,
        )
        ranked = [
            (int(document_id), float(score))
            for document_id, score in zip(
                results.documents[0], results.scores[0], strict=True
            )
            if int(document_id) in allowed_ids and float(score) > 0
        ]
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked[:candidate_k]

    def _response(
        self,
        *,
        rows: Sequence[tuple[int, float | None, float, float, float]],
        source: SearchSource,
        mode: SearchMode,
    ) -> ToolSearchResponse:
        hits = []
        for rank, (
            document_id,
            bm25_score,
            lexical_score,
            schema_score,
            relevance_score,
        ) in enumerate(rows, start=1):
            document = self._documents[document_id]
            hits.append(
                ToolSearchHit(
                    tool_name=document.tool_name,
                    tool_version=document.tool_version,
                    description=document.description,
                    required_arguments=list(document.required_arguments),
                    rank=rank,
                    sources=[source],
                    bm25_score=bm25_score,
                    lexical_score=lexical_score,
                    schema_score=schema_score,
                    relevance_score=relevance_score,
                )
            )
        return ToolSearchResponse(
            catalog_digest=self.catalog_digest,
            mode=mode,
            hits=hits,
        )


def _meaningful_tokens(tokens: Sequence[str]) -> frozenset[str]:
    meaningful = frozenset(
        token for token in tokens if token.isascii() or len(token) > 1
    )
    return meaningful or frozenset(tokens)


def _token_coverage(query_tokens: frozenset[str], document_tokens: Sequence[str]) -> float:
    if not query_tokens:
        return 0.0
    searchable = _meaningful_tokens(document_tokens)
    return len(query_tokens.intersection(searchable)) / len(query_tokens)
