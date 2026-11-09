from __future__ import annotations

import time
from typing import Any

import numpy as np

from mobile_retrieval import mobile_l2_indices
from portable_runtime import deterministic_model_preprocess, distribution_median, normalize_context_y


def portable_onnx_predict_target(
    session: Any,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_eval: np.ndarray,
    permutations: list[list[int]],
    *,
    context_size: int,
) -> tuple[np.ndarray, dict[str, float | int]]:
    """Run one target through the actual fixed-batch encoder ONNX graph."""

    started = time.perf_counter()
    train = np.asarray(x_train, dtype=np.float32)
    targets = np.asarray(y_train, dtype=np.float32)
    queries = np.asarray(x_eval, dtype=np.float32)
    batch = int(session.get_inputs()[0].shape[0])
    if batch < 1:
        raise ValueError("ONNX graph must have a fixed positive batch")
    indices = mobile_l2_indices(train, queries, context_size)
    retrieval_seconds = time.perf_counter() - started
    normalized_targets, global_mean, global_std = normalize_context_y(targets)
    member_outputs = []
    inference_seconds = 0.0

    for member, permutation_values in enumerate(permutations):
        permutation = np.asarray(permutation_values, dtype=np.int64)
        member_logits = []
        for start in range(0, len(queries), batch):
            end = min(len(queries), start + batch)
            count = end - start
            x_src = np.zeros((batch, context_size + 1, 128), dtype=np.float32)
            y_src = np.zeros((batch, context_size), dtype=np.float32)
            for local in range(batch):
                row = min(start + local, len(queries) - 1)
                selected = indices[row]
                x_src[local, :context_size, : len(permutation)] = train[selected][:, permutation]
                x_src[local, context_size, : len(permutation)] = queries[row, permutation]
                x_src[local] = deterministic_model_preprocess(x_src[local], context_size)
                y_src[local] = normalized_targets[selected]
            inference_started = time.perf_counter()
            logits = session.run(["logits"], {"x_src": x_src, "y_src": y_src})[0]
            inference_seconds += time.perf_counter() - inference_started
            member_logits.append(logits[0, :count, 16:])
        member_outputs.append(np.concatenate(member_logits, axis=0))

    averaged = np.mean(np.stack(member_outputs), axis=0, dtype=np.float32)
    base_borders = np.linspace(-10.0, 10.0, 2049, dtype=np.float32)
    prediction = np.empty(len(queries), dtype=np.float32)
    borders = base_borders * global_std + global_mean
    for row in range(len(queries)):
        prediction[row] = distribution_median(averaged[row], borders)
    total_seconds = time.perf_counter() - started
    return prediction, {
        "rows": len(queries),
        "batch": batch,
        "ensembles": len(permutations),
        "retrieval_seconds": retrieval_seconds,
        "inference_seconds": inference_seconds,
        "total_seconds": total_seconds,
    }

