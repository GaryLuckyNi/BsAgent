from collections.abc import Iterable, Sequence
from typing import Protocol

import numpy as np
from fastembed import TextEmbedding


class ToolTextEmbedder(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


class FastEmbedToolTextEmbedder:
    def __init__(
        self,
        *,
        model_name: str,
        cache_dir: str,
        local_files_only: bool,
    ) -> None:
        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=cache_dir,
            local_files_only=local_files_only,
        )

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        return normalize_embedding_matrix(self._model.embed(list(texts)))

    def embed_query(self, text: str) -> np.ndarray:
        matrix = normalize_embedding_matrix(self._model.query_embed(text))
        if len(matrix) != 1:
            raise ValueError("Query embedding must contain exactly one vector.")
        return matrix[0]


def normalize_embedding_matrix(vectors: Iterable[Iterable[float]]) -> np.ndarray:
    matrix = np.asarray(list(vectors), dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError("Embedding output must be a two-dimensional matrix.")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    normalized = np.divide(
        matrix,
        norms,
        out=np.zeros_like(matrix),
        where=norms > 0,
    )
    normalized.setflags(write=False)
    return normalized
