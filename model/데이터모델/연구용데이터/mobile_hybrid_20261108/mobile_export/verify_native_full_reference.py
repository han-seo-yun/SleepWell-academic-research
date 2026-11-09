"""Compare the complete portable path with TabDPTRegressor.predict(output_type='full')."""
from __future__ import annotations

import argparse
import json
import types
from pathlib import Path

import numpy as np
import torch
import tabdpt.model as tabdpt_model
from tabdpt.bar_distribution import distribution_median
from tabdpt.regressor import TabDPTRegressor

from mobile_retrieval import bind_mobile_retrieval
from portable_predict import portable_predict_fitted
from portable_runtime import deterministic_model_preprocess


def bind_external_preprocess_to_native_forward(model, context_size: int) -> None:
    """Keep native model.forward, replacing only its preprocessing with frozen external math."""
    original_forward = model.forward

    def forward(self, x_src, y_src, is_cls):
        values = x_src.detach().cpu().numpy().astype(np.float32, copy=True)
        for row in range(len(values)):
            values[row] = deterministic_model_preprocess(values[row], context_size)
        preprocessed = torch.from_numpy(values).to(device=x_src.device)
        original_clip = tabdpt_model.clip_outliers
        original_normalize = tabdpt_model.normalize_data
        identity = lambda tensor, *args, **kwargs: tensor
        tabdpt_model.clip_outliers = identity
        tabdpt_model.normalize_data = identity
        try:
            return original_forward(preprocessed, y_src, is_cls)
        finally:
            tabdpt_model.clip_outliers = original_clip
            tabdpt_model.normalize_data = original_normalize

    model.forward = types.MethodType(forward, model)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--context", type=int, default=1024)
    parser.add_argument("--ensembles", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261107)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    result = {"cohorts": {}, "method": "native full predict with deterministic retrieval and external preprocessing"}
    errors = []
    for cohort in ("cmi", "mesa"):
        with np.load(args.cache_dir / f"{cohort}.npz", allow_pickle=False) as stored:
            data = {name: stored[name].copy() for name in stored.files}
        train = data["splits"] == "train"
        validation_x = data["X_hybrid"][data["splits"] == "validation"]
        positions = np.asarray([0, len(validation_x) - 1], dtype=int)
        x_eval = validation_x[positions]
        channel_records = []
        for channel in range(2):
            estimator = TabDPTRegressor(
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
            estimator.fit(data["X_hybrid"][train], data["y"][train, channel])
            bind_mobile_retrieval(estimator)
            bind_external_preprocess_to_native_forward(estimator.model, args.context)
            with torch.inference_mode():
                native_full = estimator.predict(
                    x_eval,
                    n_ensembles=args.ensembles,
                    context_size=args.context,
                    batch_size=1,
                    seed=args.seed,
                    output_type="full",
                )
            native = distribution_median(native_full)
            portable = portable_predict_fitted(
                estimator,
                x_eval,
                context_size=args.context,
                n_ensembles=args.ensembles,
                seed=args.seed,
                batch_size=1,
            )
            difference = np.abs(np.asarray(native) - np.asarray(portable))
            errors.extend(difference.tolist())
            channel_records.append(
                {
                    "channel": channel,
                    "native_median": np.asarray(native).tolist(),
                    "portable_median": np.asarray(portable).tolist(),
                    "absolute_error_min": difference.tolist(),
                    "native_borders_first_last": [
                        float(native_full["borders"][0]),
                        float(native_full["borders"][-1]),
                    ],
                }
            )
        result["cohorts"][cohort] = {
            "validation_positions": positions.tolist(),
            "channels": channel_records,
        }
    result.update(
        {
            "max_abs_error_min": float(max(errors)),
            "mean_abs_error_min": float(np.mean(errors)),
            "prediction_count": len(errors),
            "status": "diagnostic_complete",
            "interpretation": "Four first/last Validation-row medians quantify independent full-path agreement; this is not an accuracy or adoption gate.",
        }
    )
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
