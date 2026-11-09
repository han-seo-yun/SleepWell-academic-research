from __future__ import annotations

from typing import Any

import numpy as np
import torch

from mobile_retrieval import mobile_l2_indices
from portable_runtime import deterministic_model_preprocess, distribution_median, normalize_context_y
from probe_export import PreprocessedRegressionForward, patch_exportable_attention


METHOD_ID = "tabdpt13_mobile_fp32_external_preprocess_think64_v2"


def ensemble_seeds(seed: int, n_ensembles: int) -> list[int]:
    if n_ensembles == 1:
        return [seed]
    return [int(value) for value in np.random.SeedSequence(seed).generate_state(n_ensembles)]


def feature_permutations(feature_count: int, seed: int, n_ensembles: int) -> list[np.ndarray]:
    output = []
    for inner_seed in ensemble_seeds(seed, n_ensembles):
        generator = torch.Generator().manual_seed(inner_seed)
        output.append(torch.randperm(feature_count, generator=generator).numpy())
    return output


def portable_predict_fitted(
    estimator: Any,
    x_eval: np.ndarray,
    *,
    context_size: int,
    n_ensembles: int,
    seed: int,
    batch_size: int = 16,
) -> np.ndarray:
    """Predict one fitted TabDPT target with the frozen mobile math contract."""

    if not getattr(estimator, "is_fitted_", False):
        raise ValueError("estimator must be fitted")
    if estimator.normalizer is not None or estimator.missing_indicators:
        raise ValueError("portable contract requires normalizer=None and missing_indicators=False")
    if not 1 <= context_size <= len(estimator.X_train):
        raise ValueError("invalid context_size")
    if n_ensembles not in (1, 2):
        raise ValueError("portable contract supports one or two ensembles")

    queries = estimator.imputer.transform(np.asarray(x_eval)).astype(np.float32)
    train = np.asarray(estimator.X_train, dtype=np.float32)
    targets = np.asarray(estimator.y_train, dtype=np.float32)
    indices = mobile_l2_indices(train, queries, context_size)
    permutations = feature_permutations(train.shape[1], seed, n_ensembles)
    patch_exportable_attention(estimator.model)
    wrapper = PreprocessedRegressionForward(estimator.model).eval()
    all_member_logits: list[np.ndarray] = []
    normalized_targets, global_mean, global_std = normalize_context_y(targets)

    for member, permutation in enumerate(permutations):
        member_logits = []
        for start in range(0, len(queries), batch_size):
            end = min(len(queries), start + batch_size)
            count = end - start
            x_src = np.zeros((count, context_size + 1, 128), dtype=np.float32)
            y_src = np.empty((count, context_size), dtype=np.float32)
            for local, row in enumerate(range(start, end)):
                selected = indices[row]
                x_src[local, :context_size, : len(permutation)] = train[selected][:, permutation]
                x_src[local, context_size, : len(permutation)] = queries[row, permutation]
                x_src[local] = deterministic_model_preprocess(x_src[local], context_size)
                y_src[local] = normalized_targets[selected]
            with torch.inference_mode():
                logits = wrapper(
                    torch.from_numpy(x_src).to(estimator.device),
                    torch.from_numpy(y_src).to(estimator.device),
                )[:, :, estimator.max_num_classes :]
            member_logits.append(logits.squeeze(0).detach().cpu().numpy())
        all_member_logits.append(np.concatenate(member_logits, axis=0))

    averaged_logits = np.mean(np.stack(all_member_logits), axis=0, dtype=np.float32)
    base_borders = np.linspace(-10.0, 10.0, 2049, dtype=np.float32)
    prediction = np.empty(len(queries), dtype=np.float32)
    borders = base_borders * global_std + global_mean
    for row in range(len(queries)):
        prediction[row] = distribution_median(averaged_logits[row], borders)
    return prediction

