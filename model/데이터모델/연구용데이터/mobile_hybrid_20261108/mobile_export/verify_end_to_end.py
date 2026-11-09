from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
from tabdpt import TabDPTRegressor, distribution_median

from probe_export import PreprocessedRegressionForward, patch_exportable_attention
from mobile_retrieval import bind_mobile_retrieval, mobile_l2_indices
from portable_runtime import (
    PortableTabDptRuntime,
    deterministic_model_preprocess,
    distribution_median as portable_median,
    normalize_context_y,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    args = parser.parse_args()

    runtime = PortableTabDptRuntime(args.bundle)
    context_size = int(runtime.manifest["context_size"])
    ensembles = int(runtime.manifest["ensembles"])
    seed = int(runtime.manifest["seed"])
    result: dict[str, object] = {"cohorts": {}}
    for cohort in ("cmi", "mesa"):
        with np.load(args.cache_dir / f"{cohort}.npz", allow_pickle=False) as data:
            train_mask = data["splits"] == "train"
            test_index = int(np.flatnonzero(data["splits"] == "test")[0])
            x_train = data["X_hybrid"][train_mask]
            y_train = data["y"][train_mask]
            transformed_query = data["X_hybrid"][test_index]
            raw_query = data["X_hybrid_raw"][test_index]

        portable_transform = runtime.transform_raw(cohort, raw_query)
        transform_error = float(
            np.max(np.abs(portable_transform - transformed_query.astype(np.float32)))
        )
        portable_prediction = runtime.predict_transformed(cohort, portable_transform)
        reference_prediction = []
        estimator = bind_mobile_retrieval(
            TabDPTRegressor(
                normalizer=None,
                missing_indicators=False,
                clip_sigma=8.0,
                feature_reduction="pca",
                context_reduction="retrieval",
                device="cpu",
                use_flash=False,
                compile=False,
                model_weight_path=str(args.weights),
                verbose=False,
            )
        )
        for channel in range(2):
            estimator.fit(x_train, y_train[:, channel])
            full = estimator.predict(
                transformed_query[None, :],
                n_ensembles=ensembles,
                context_size=context_size,
                batch_size=1,
                seed=seed,
                output_type="full",
            )
            reference_prediction.append(float(distribution_median(full)[0]))
        reference = np.asarray(reference_prediction)
        patch_exportable_attention(estimator.model)
        preprocessed_wrapper = PreprocessedRegressionForward(estimator.model).eval()
        x_train32, y_train32 = runtime.contexts[cohort]
        mobile_indices = mobile_l2_indices(
            x_train32, portable_transform[None, :], context_size
        )[0]
        portable_python_prediction = []
        for channel in range(2):
            normalized_full_y, mean_y, std_y = normalize_context_y(y_train32[:, channel])
            normalized_y = normalized_full_y[mobile_indices]
            logits_members = []
            for permutation in runtime.manifest["cohorts"][cohort]["feature_permutations"]:
                permutation = np.asarray(permutation, dtype=np.int64)
                portable_x = np.zeros((1, context_size + 1, 128), dtype=np.float32)
                portable_x[0, :context_size, : len(permutation)] = x_train32[mobile_indices][:, permutation]
                portable_x[0, context_size, : len(permutation)] = portable_transform[permutation]
                portable_x[0] = deterministic_model_preprocess(portable_x[0], context_size)
                with torch.inference_mode():
                    logits_members.append(
                        preprocessed_wrapper(
                            torch.from_numpy(portable_x),
                            torch.from_numpy(normalized_y[None, :]),
                        ).numpy()[0, 0, 16:]
                    )
            averaged = np.mean(np.stack(logits_members), axis=0, dtype=np.float32)
            borders = np.linspace(-10.0, 10.0, 2049, dtype=np.float32) * std_y + mean_y
            portable_python_prediction.append(portable_median(averaged, borders))
        portable_python = np.asarray(portable_python_prediction)
        result["cohorts"][cohort] = {
            "raw_transform_max_abs_error": transform_error,
            "portable_prediction": portable_prediction.tolist(),
            "patched_tabdpt_reference": reference.tolist(),
            "prediction_abs_error": np.abs(portable_prediction - reference).tolist(),
            "prediction_max_abs_error": float(np.max(np.abs(portable_prediction - reference))),
            "portable_python_prediction": portable_python.tolist(),
            "onnx_vs_portable_python_abs_error": np.abs(
                portable_prediction - portable_python
            ).tolist(),
            "onnx_vs_portable_python_max_abs_error": float(
                np.max(np.abs(portable_prediction - portable_python))
            ),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
