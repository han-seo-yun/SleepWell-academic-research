from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
from pathlib import Path

import numpy as np
import torch

from portable_runtime import normalize_context_y


METHOD_ID = "mobile_l2_f32store_f64accum_index_tie_v1"
CONTEXT_MAGIC = b"SWCTX01\0"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_context(path: Path, x_train: np.ndarray, y_train: np.ndarray) -> None:
    with path.open("wb") as stream:
        stream.write(CONTEXT_MAGIC)
        stream.write(struct.pack("<iii", x_train.shape[0], x_train.shape[1], y_train.shape[1]))
        stream.write(np.ascontiguousarray(x_train, dtype="<f4").tobytes())
        stream.write(np.ascontiguousarray(y_train, dtype="<f4").tobytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", type=Path)
    parser.add_argument("--context-dir", type=Path)
    parser.add_argument("--preprocessor-dir", type=Path)
    parser.add_argument("--onnx", type=Path, required=True)
    parser.add_argument("--context", type=int, required=True)
    parser.add_argument("--ensembles", type=int, choices=(1, 2), required=True)
    parser.add_argument("--seed", type=int, default=20261107)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preprocessed-x", action="store_true")
    parser.add_argument("--thinking-rows", type=int, required=True)
    parser.add_argument("--thinking-embed-sha256", required=True)
    parser.add_argument("--architecture-parity", type=Path, required=True)
    args = parser.parse_args()

    context_dir = args.context_dir or args.private_dir
    preprocessor_dir = args.preprocessor_dir or args.private_dir
    if context_dir is None or preprocessor_dir is None:
        parser.error("provide --private-dir or both --context-dir and --preprocessor-dir")

    args.output.mkdir(parents=True, exist_ok=True)
    inner_seeds = (
        [args.seed]
        if args.ensembles == 1
        else [int(value) for value in np.random.SeedSequence(args.seed).generate_state(args.ensembles)]
    )
    cohorts: dict[str, object] = {}
    files: list[Path] = []
    for cohort in ("cmi", "mesa"):
        legacy_context = context_dir / f"{cohort}_train.npz"
        source_context = legacy_context if legacy_context.is_file() else context_dir / f"context_{cohort}.npz"
        source_preprocessor = preprocessor_dir / f"{cohort}_preprocessor.json"
        with np.load(source_context, allow_pickle=False) as data:
            x_key = "X" if "X" in data.files else "X_train"
            y_key = "y" if "y" in data.files else "y_train"
            x_train = np.asarray(data[x_key], dtype=np.float32)
            y_train = np.asarray(data[y_key], dtype=np.float32)
        context_path = args.output / f"{cohort}_context_f32.bin"
        write_context(context_path, x_train, y_train)
        preprocessor_path = args.output / source_preprocessor.name
        shutil.copyfile(source_preprocessor, preprocessor_path)
        permutations = []
        for inner_seed in inner_seeds:
            generator = torch.Generator().manual_seed(inner_seed)
            permutations.append(torch.randperm(x_train.shape[1], generator=generator).tolist())
        target_stats = []
        for channel in range(y_train.shape[1]):
            normalized, mean, std = normalize_context_y(y_train[:, channel])
            target_stats.append(
                {
                    "channel": channel,
                    "mean_float32": float(mean),
                    "std_plus_1e6_float32": float(std),
                    "normalized_full_train_sha256": hashlib.sha256(
                        np.ascontiguousarray(normalized, dtype="<f4").tobytes()
                    ).hexdigest(),
                }
            )
        cohorts[cohort] = {
            "train_rows": int(x_train.shape[0]),
            "transformed_features": int(x_train.shape[1]),
            "context_file": context_path.name,
            "context_format": "SWCTX01 little-endian: magic[8], int32 rows/features/targets, X float32 row-major, y float32 row-major",
            "context_sha256": sha256(context_path),
            "preprocessor_file": preprocessor_path.name,
            "preprocessor_sha256": sha256(preprocessor_path),
            "feature_permutations": permutations,
            "target_normalization": {
                "scope": "full_train_before_retrieval",
                "dtype": "float32",
                "ddof": 1,
                "epsilon": 1e-6,
                "channels": target_stats,
            },
        }
        files.extend([context_path, preprocessor_path])

    model_target = args.output / args.onnx.name
    shutil.copyfile(args.onnx, model_target)
    files.append(model_target)
    external_source = args.onnx.with_name(args.onnx.name + ".data")
    if external_source.exists():
        external_target = args.output / external_source.name
        shutil.copyfile(external_source, external_target)
        files.append(external_target)
    architecture_target = args.output / args.architecture_parity.name
    shutil.copyfile(args.architecture_parity, architecture_target)
    files.append(architecture_target)

    manifest = {
        "schema": "sleepwell.mobile_tabdpt_bundle.v1",
        "model": "TabDPT-v1.3-FP32",
        "model_file": model_target.name,
        "context_size": args.context,
        "ensembles": args.ensembles,
        "seed": args.seed,
        "inner_seeds": inner_seeds,
        "retrieval": {
            "method_id": METHOD_ID,
            "stored_dtype": "float32",
            "accumulator_dtype": "float64",
            "feature_order": "ascending",
            "tie_break": "ascending_train_row_index",
        },
        "model_contract": {
            "x_src": [1, args.context + 1, 128],
            "y_src": [1, args.context],
            "logits": [1, 1, 2064],
            "classification_outputs_to_drop": 16,
            "regression_bins": 2048,
            "regression_bin_min": -10.0,
            "regression_bin_max": 10.0,
            "point_estimate": "distribution_median_after_logit_ensemble_mean",
            "x_src_semantics": "deterministic_mobile_preprocessed" if args.preprocessed_x else "raw_padded_model_preprocess",
            "n_thinking_rows": args.thinking_rows,
            "thinking_embed_sha256": args.thinking_embed_sha256,
        },
        "architecture_parity": {
            "file": args.architecture_parity.name,
            "sha256": sha256(args.architecture_parity),
        },
        "cohorts": cohorts,
        "private_artifact": True,
        "source_sha256": {
            name: sha256(Path(__file__).resolve().parent / name)
            for name in (
                "mobile_retrieval.py",
                "portable_runtime.py",
                "probe_export.py",
                "portable_predict.py",
                "export_onnx.py",
                "build_bundle.py",
            )
        },
        "files": [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in files
        ],
    }
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
