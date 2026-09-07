import numpy as np

from app.tools.search.embedding import normalize_embedding_matrix


def test_normalize_embedding_matrix_returns_read_only_float32_rows():
    matrix = normalize_embedding_matrix([[3.0, 4.0], [0.0, 0.0]])

    assert matrix.dtype == np.float32
    assert np.allclose(matrix[0], [0.6, 0.8])
    assert np.allclose(matrix[1], [0.0, 0.0])
    assert matrix.flags.writeable is False
