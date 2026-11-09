from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from tabdpt.utils import FAISS


def android_float32_distances(train: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Emulate a Kotlin Float loop's left-to-right squared-L2 accumulation."""

    train32 = np.asarray(train, dtype=np.float32)
    query32 = np.asarray(query, dtype=np.float32)
    distances = np.zeros(train32.shape[0], dtype=np.float32)
    for feature in range(train32.shape[1]):
        delta = train32[:, feature] - query32[feature]
        distances += delta * delta
    return distances


def ordered_topk(distances: np.ndarray, k: int) -> np.ndarray:
    indices = np.arange(len(distances), dtype=np.int64)
    return np.lexsort((indices, distances))[:k]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--contexts", type=int, nargs="+", default=[256, 512, 1024])
    parser.add_argument("--queries", type=int, default=64)
    args = parser.parse_args()

    result: dict[str, object] = {"cohorts": {}}
    for cohort in ("cmi", "mesa"):
        with np.load(args.cache_dir / f"{cohort}.npz", allow_pickle=False) as data:
            train_mask = data["splits"] == "train"
            train = data["X_hybrid"][train_mask]
            train_y = data["y"][train_mask]
            queries = data["X_hybrid"][data["splits"] == "test"][: args.queries]
        faiss = FAISS(train)
        cohort_result: dict[str, object] = {
            "train_shape": list(train.shape),
            "query_count": int(len(queries)),
            "contexts": {},
        }
        max_context = min(max(args.contexts), len(train))
        faiss_indices = faiss.get_knn_indices(queries, max_context)
        mobile_indices = np.stack(
            [ordered_topk(android_float32_distances(train, query), max_context) for query in queries]
        )
        for context in args.contexts:
            if context > len(train):
                continue
            mismatched_rows = np.flatnonzero(
                np.any(faiss_indices[:, :context] != mobile_indices[:, :context], axis=1)
            )
            set_mismatched_rows = np.array(
                [
                    row
                    for row in range(len(queries))
                    if set(faiss_indices[row, :context].tolist())
                    != set(mobile_indices[row, :context].tolist())
                ],
                dtype=np.int64,
            )
            first_difference = None
            if len(mismatched_rows):
                row = int(mismatched_rows[0])
                positions = np.flatnonzero(
                    faiss_indices[row, :context] != mobile_indices[row, :context]
                )
                position = int(positions[0])
                first_difference = {
                    "query": row,
                    "position": position,
                    "faiss_index": int(faiss_indices[row, position]),
                    "mobile_index": int(mobile_indices[row, position]),
                }
            first_set_difference = None
            if len(set_mismatched_rows):
                row = int(set_mismatched_rows[0])
                faiss_set = set(faiss_indices[row, :context].tolist())
                mobile_set = set(mobile_indices[row, :context].tolist())
                only_faiss = sorted(faiss_set - mobile_set)
                only_mobile = sorted(mobile_set - faiss_set)
                f_idx, m_idx = only_faiss[0], only_mobile[0]
                distances = android_float32_distances(train, queries[row])
                first_set_difference = {
                    "query": row,
                    "only_faiss_count": len(only_faiss),
                    "only_mobile_count": len(only_mobile),
                    "faiss_example_index": f_idx,
                    "mobile_example_index": m_idx,
                    "android_distance_faiss_example": float(distances[f_idx]),
                    "android_distance_mobile_example": float(distances[m_idx]),
                    "feature_max_abs_delta_between_examples": float(
                        np.max(np.abs(train[f_idx] - train[m_idx]))
                    ),
                    "target_abs_delta_between_examples": np.abs(
                        train_y[f_idx] - train_y[m_idx]
                    ).tolist(),
                }
            cohort_result["contexts"][str(context)] = {
                "exact_rows": int(len(queries) - len(mismatched_rows)),
                "mismatched_rows": int(len(mismatched_rows)),
                "same_neighbor_set_different_order_rows": int(
                    len(mismatched_rows) - len(set_mismatched_rows)
                ),
                "different_neighbor_set_rows": int(len(set_mismatched_rows)),
                "first_difference": first_difference,
                "first_set_difference": first_set_difference,
            }
        result["cohorts"][cohort] = cohort_result
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
