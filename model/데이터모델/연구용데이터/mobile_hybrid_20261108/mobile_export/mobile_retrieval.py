from __future__ import annotations

import types
from typing import Any

import numpy as np


def mobile_l2_indices(train: np.ndarray, queries: np.ndarray, k: int) -> np.ndarray:
    """Deterministic Android-compatible squared-L2 top-k.

    The contract stores transformed train/query features as IEEE float32, casts
    each value to IEEE float64 for distance arithmetic, accumulates features in
    ascending feature order, and ranks by ``(distance, train_row_index)``.
    """

    train32 = np.ascontiguousarray(train, dtype=np.float32)
    query32 = np.ascontiguousarray(queries, dtype=np.float32)
    if train32.ndim != 2 or query32.ndim != 2:
        raise ValueError("train and queries must be 2D")
    if train32.shape[1] != query32.shape[1]:
        raise ValueError("train and query feature counts differ")
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= len(train32):
        raise ValueError("k must be an integer in [1, n_train]")

    row_indices = np.arange(len(train32), dtype=np.int64)
    result = np.empty((len(query32), k), dtype=np.int64)
    train64 = train32.astype(np.float64)
    for row, query in enumerate(query32):
        query64 = query.astype(np.float64)
        distances = np.zeros(len(train64), dtype=np.float64)
        for feature in range(train64.shape[1]):
            delta = train64[:, feature] - query64[feature]
            distances += delta * delta
        result[row] = np.lexsort((row_indices, distances))[:k]
    return result


def bind_mobile_retrieval(estimator: Any) -> Any:
    """Replace only a fitted TabDPT estimator's retrieval index selection."""

    def get_indices(self: Any, x_test: np.ndarray, context_size: int, seed: int | None = None) -> np.ndarray:
        del seed
        return mobile_l2_indices(self.X_train, x_test, context_size)

    estimator._get_faiss_knn_indices = types.MethodType(get_indices, estimator)
    estimator.faiss_knn = None
    return estimator

