import numpy as np

from app.tools.search.embedding import CachingToolTextEmbedder, normalize_embedding_matrix


def test_normalize_embedding_matrix_returns_read_only_float32_rows():
    matrix = normalize_embedding_matrix([[3.0, 4.0], [0.0, 0.0]])

    assert matrix.dtype == np.float32
    assert np.allclose(matrix[0], [0.6, 0.8])
    assert np.allclose(matrix[1], [0.0, 0.0])
    assert matrix.flags.writeable is False


def test_query_embedding_cache_is_bounded_and_normalizes_query_key():
    class _Embedder:
        def __init__(self):
            self.query_calls = 0

        def embed_documents(self, texts):
            return np.zeros((len(texts), 2), dtype=np.float32)

        def embed_query(self, text):
            self.query_calls += 1
            return np.asarray([self.query_calls, 1], dtype=np.float32)

    delegate = _Embedder()
    embedder = CachingToolTextEmbedder(
        delegate,
        catalog_digest="digest-1",
        max_entries=2,
    )

    first = embedder.embed_query("  利润\n表 ")
    second = embedder.embed_query("利润 表")
    embedder.embed_query("日线")
    embedder.embed_query("公告")
    embedder.embed_query("利润 表")

    assert np.array_equal(first, second)
    assert delegate.query_calls == 4
