from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import bm25s
import numpy as np

from app.schemas.tools import ToolSpec
from app.tools.search.contracts import SearchSource, ToolSearchHit, ToolSearchResponse
from app.tools.search.documents import (
    ToolSearchDocument,
    build_tool_search_document,
    tokenize_tool_text,
)
from app.tools.search.embedding import ToolTextEmbedder, normalize_embedding_matrix


_SOURCE_ORDER: dict[SearchSource, int] = {"exact": 0, "bm25": 1, "dense": 2}


class ToolSearchUnavailableError(RuntimeError):
    code = "tool_search_unavailable"

    def __init__(self) -> None:
        super().__init__("工具检索暂不可用。")


@dataclass(frozen=True)
class ToolSearchIndex:
    catalog_digest: str
    _documents: tuple[ToolSearchDocument, ...]
    _bm25: bm25s.BM25 | None
    _document_embeddings: np.ndarray
    _embedder: ToolTextEmbedder

    @classmethod
    def build(
        cls,
        *,
        specs: Sequence[ToolSpec],
        catalog_digest: str,
        embedder: ToolTextEmbedder,
    ) -> "ToolSearchIndex":
        documents = tuple(build_tool_search_document(spec) for spec in specs)
        if not documents:
            empty = np.empty((0, 0), dtype=np.float32)
            empty.setflags(write=False)
            return cls(
                catalog_digest=catalog_digest,
                _documents=(),
                _bm25=None,
                _document_embeddings=empty,
                _embedder=embedder,
            )

        retriever = bm25s.BM25()
        retriever.index(
            [list(document.tokens) for document in documents],
            show_progress=False,
        )
        embeddings = normalize_embedding_matrix(
            embedder.embed_documents([document.search_text for document in documents])
        )
        if len(embeddings) != len(documents):
            raise ValueError("Document embedding count does not match tool catalog.")
        return cls(
            catalog_digest=catalog_digest,
            _documents=documents,
            _bm25=retriever,
            _document_embeddings=embeddings,
            _embedder=embedder,
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
            return ToolSearchResponse(
                catalog_digest=self.catalog_digest,
                mode="hybrid",
            )

        allowed_ids = self._allowed_document_ids(allowed_tool_names)
        exact_ids = self._exact_matches(query, allowed_ids=allowed_ids)
        if exact_ids:
            return self._response(
                document_ids=exact_ids[:top_k],
                sources={document_id: ("exact",) for document_id in exact_ids},
                mode="exact",
            )

        bm25_error = False
        dense_error = False
        try:
            bm25_ranking = self._bm25_ranking(
                query,
                allowed_ids=allowed_ids,
                candidate_k=candidate_k,
            )
        except Exception:
            bm25_error = True
            bm25_ranking = []
        try:
            dense_ranking = self._dense_ranking(
                query,
                allowed_ids=allowed_ids,
                candidate_k=candidate_k,
            )
        except Exception:
            dense_error = True
            dense_ranking = []

        if bm25_error and dense_error:
            raise ToolSearchUnavailableError()
        rankings: dict[SearchSource, Sequence[int]] = {}
        if not bm25_error:
            rankings["bm25"] = bm25_ranking
        if not dense_error:
            rankings["dense"] = dense_ranking
        fused = reciprocal_rank_fusion(rankings)
        selected = [document_id for document_id, _, _ in fused[:top_k]]
        source_map = {
            document_id: sources for document_id, _, sources in fused[:top_k]
        }
        return self._response(
            document_ids=selected,
            sources=source_map,
            mode="bm25_fallback" if dense_error else "hybrid",
            degraded=bm25_error or dense_error,
        )

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
    ) -> list[int]:
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
        return [document_id for document_id, _ in ranked[:candidate_k]]

    def _dense_ranking(
        self,
        query: str,
        *,
        allowed_ids: frozenset[int],
        candidate_k: int,
    ) -> list[int]:
        if not allowed_ids:
            return []
        query_matrix = normalize_embedding_matrix([self._embedder.embed_query(query)])
        query_vector = query_matrix[0]
        if not np.any(query_vector):
            return []
        scores = self._document_embeddings @ query_vector
        ranked = sorted(
            ((index, float(scores[index])) for index in allowed_ids),
            key=lambda item: (-item[1], item[0]),
        )
        return [document_id for document_id, _ in ranked[:candidate_k]]

    def _response(
        self,
        *,
        document_ids: Sequence[int],
        sources: Mapping[int, Sequence[SearchSource]],
        mode: str,
        degraded: bool = False,
    ) -> ToolSearchResponse:
        hits = []
        for rank, document_id in enumerate(document_ids, start=1):
            document = self._documents[document_id]
            hits.append(
                ToolSearchHit(
                    tool_name=document.tool_name,
                    tool_version=document.tool_version,
                    description=document.description,
                    required_arguments=list(document.required_arguments),
                    rank=rank,
                    sources=list(sources[document_id]),
                )
            )
        return ToolSearchResponse(
            catalog_digest=self.catalog_digest,
            mode=mode,
            hits=hits,
            degraded=degraded,
        )


def reciprocal_rank_fusion(
    rankings: Mapping[SearchSource, Sequence[int]],
    *,
    rrf_k: int = 60,
) -> list[tuple[int, float, tuple[SearchSource, ...]]]:
    scores: dict[int, float] = {}
    sources: dict[int, set[SearchSource]] = {}
    for source, document_ids in rankings.items():
        for rank, document_id in enumerate(document_ids, start=1):
            scores[document_id] = scores.get(document_id, 0.0) + 1 / (rrf_k + rank)
            sources.setdefault(document_id, set()).add(source)
    return sorted(
        (
            (
                document_id,
                score,
                tuple(sorted(sources[document_id], key=_SOURCE_ORDER.__getitem__)),
            )
            for document_id, score in scores.items()
        ),
        key=lambda item: (-item[1], item[0]),
    )
