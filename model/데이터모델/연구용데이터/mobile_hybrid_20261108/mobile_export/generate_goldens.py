from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import torch
from tabdpt.regressor import TabDPTRegressor

from mobile_retrieval import mobile_l2_indices
from portable_runtime import (
    PortableTabDptRuntime,
    deterministic_model_preprocess,
    distribution_median,
    normalize_context_y,
)
from probe_export import PreprocessedRegressionForward, patch_exportable_attention


def digest_bytes(values: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(values).tobytes()).hexdigest()


def constrain(values: np.ndarray) -> np.ndarray:
    result = np.asarray(values, dtype=np.float64).reshape(1, 2).copy()
    result[:, 0] = np.clip(result[:, 0], 0.0, 960.0)
    result[:, 1] = np.minimum(
        1440.0,
        np.maximum(result[:, 0] + 30.0, np.minimum(result[:, 0] + 960.0, result[:, 1])),
    )
    return result[0]


def selected_validation_rows(cache_dir: Path) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    combined_train = []
    for cohort in ("cmi", "mesa"):
        with np.load(cache_dir / f"{cohort}.npz", allow_pickle=False) as data:
            validation = data["X_hybrid_raw"][data["splits"] == "validation"]
            train = data["X_hybrid_raw"][data["splits"] == "train"]
        combined_train.append(train)
        positions = np.linspace(0, len(validation) - 1, 14, dtype=int)
        for sequence, position in enumerate(positions):
            cases.append(
                {
                    "id": f"{cohort}_validation_uniform_{sequence:02d}",
                    "source": f"{cohort}/validation/features_only",
                    "raw39": validation[position].tolist(),
                }
            )
    pooled = np.vstack(combined_train)
    cases.extend(
        [
            {"id": "corner_zero", "source": "synthetic", "raw39": np.zeros(39).tolist()},
            {"id": "corner_train_median", "source": "train_summary", "raw39": np.median(pooled, axis=0).tolist()},
            {"id": "corner_high", "source": "synthetic", "raw39": np.full(39, 1_000_000.0).tolist()},
            {"id": "corner_alternating", "source": "synthetic", "raw39": np.where(np.arange(39) % 2, -1_000_000.0, 1_000_000.0).tolist()},
        ]
    )
    return cases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    runtime = PortableTabDptRuntime(args.bundle)
    estimator = TabDPTRegressor(
        normalizer=None,
        device="cpu",
        use_flash=False,
        compile=False,
        model_weight_path=str(args.weights),
        verbose=False,
    )
    patch_exportable_attention(estimator.model)
    pytorch_model = PreprocessedRegressionForward(estimator.model).eval()
    k = int(runtime.manifest["context_size"])
    base_borders = np.linspace(-10.0, 10.0, 2049, dtype=np.float32)
    output_cases = []

    for case in selected_validation_rows(args.cache_dir):
        raw39 = np.asarray(case["raw39"], dtype=np.float64)
        case_result = {**case, "raw39_sha256": digest_bytes(raw39), "cohorts": {}}
        for cohort in ("cmi", "mesa"):
            query = runtime.transform_raw(cohort, raw39)
            x_train, y_train = runtime.contexts[cohort]
            indices = mobile_l2_indices(x_train, query[None], k)[0]
            metadata = runtime.manifest["cohorts"][cohort]
            onnx_channels = []
            pytorch_channels = []
            tensor_records = []
            for channel in range(2):
                normalized_full_y, mean_y, std_y = normalize_context_y(y_train[:, channel])
                normalized_y = normalized_full_y[indices]
                onnx_members = []
                pytorch_members = []
                member_records = []
                for permutation_values in metadata["feature_permutations"]:
                    permutation = np.asarray(permutation_values, dtype=np.int64)
                    x_src = np.zeros((1, k + 1, 128), dtype=np.float32)
                    x_src[0, :k, : len(permutation)] = x_train[indices][:, permutation]
                    x_src[0, k, : len(permutation)] = query[permutation]
                    x_src[0] = deterministic_model_preprocess(x_src[0], k)
                    onnx_logits = runtime.session.run(
                        ["logits"], {"x_src": x_src, "y_src": normalized_y[None, :]}
                    )[0][0, 0, 16:]
                    with torch.inference_mode():
                        pytorch_logits = pytorch_model(
                            torch.from_numpy(x_src), torch.from_numpy(normalized_y[None, :])
                        ).numpy()[0, 0, 16:]
                    onnx_members.append(onnx_logits)
                    pytorch_members.append(pytorch_logits)
                    member_records.append(
                        {
                            "x_src_sha256": digest_bytes(x_src),
                            "y_src_sha256": digest_bytes(normalized_y[None, :]),
                            "logits_max_abs_error_onnx_vs_pytorch": float(
                                np.max(np.abs(onnx_logits - pytorch_logits))
                            ),
                        }
                    )
                borders = base_borders * std_y + mean_y
                onnx_channels.append(
                    distribution_median(np.mean(np.stack(onnx_members), axis=0, dtype=np.float32), borders)
                )
                pytorch_channels.append(
                    distribution_median(np.mean(np.stack(pytorch_members), axis=0, dtype=np.float32), borders)
                )
                tensor_records.append(member_records)
            onnx_array = np.asarray(onnx_channels)
            pytorch_array = np.asarray(pytorch_channels)
            case_result["cohorts"][cohort] = {
                "transformed_sha256": digest_bytes(query),
                "retrieval_indices": indices.tolist(),
                "retrieval_indices_sha256": digest_bytes(indices.astype("<i8")),
                "portable_python_onnx_raw": onnx_array.tolist(),
                "portable_python_onnx_constrained": constrain(onnx_array).tolist(),
                "portable_pytorch_raw": pytorch_array.tolist(),
                "portable_pytorch_constrained": constrain(pytorch_array).tolist(),
                "prediction_abs_error_onnx_vs_pytorch": np.abs(onnx_array - pytorch_array).tolist(),
                "tensors_by_channel_then_member": tensor_records,
            }
        output_cases.append(case_result)

    payload = {
        "schema": "sleepwell.mobile_tabdpt_goldens.v1",
        "bundle_manifest_sha256": hashlib.sha256((args.bundle / "manifest.json").read_bytes()).hexdigest(),
        "case_selection": "14 uniformly spaced Validation feature rows per cohort plus four label-free corners; no Test rows",
        "case_count": len(output_cases),
        "cases": output_cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"case_count": len(output_cases), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
