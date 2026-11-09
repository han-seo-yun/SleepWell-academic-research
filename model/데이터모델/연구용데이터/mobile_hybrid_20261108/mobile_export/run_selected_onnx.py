"""Run the frozen deployable TabDPT ONNX graph on one locked split cell.

The input cache already contains the Train-fitted cohort transformation.  This
runner changes only the retrieval and encoder preprocessing to the explicitly
portable contracts implemented in this directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import torch  # noqa: F401  # Preload matching CUDA/cuDNN runtime DLLs before ORT import.

from portable_onnx_predict import portable_onnx_predict_target


METHOD_ID = "tabdpt13_mobile_fp32_external_preprocess_think64_v2"
RETRIEVAL_ID = "mobile_l2_f32store_f64accum_index_tie_v1"
SEED = 20261107


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def constrain_predictions(values: np.ndarray) -> np.ndarray:
    result = np.asarray(values, dtype=np.float64).copy()
    if result.ndim != 2 or result.shape[1] != 2 or not np.isfinite(result).all():
        raise ValueError("predictions must be a finite n-by-2 array")
    result[:, 0] = np.clip(result[:, 0], 0.0, 960.0)
    result[:, 1] = np.minimum(
        1440.0,
        np.maximum(result[:, 0] + 30.0, np.minimum(result[:, 0] + 960.0, result[:, 1])),
    )
    return result


def permutations(features: int, ensembles: int) -> list[list[int]]:
    seeds = (
        [SEED]
        if ensembles == 1
        else [int(value) for value in np.random.SeedSequence(SEED).generate_state(ensembles)]
    )
    import torch

    return [
        torch.randperm(features, generator=torch.Generator().manual_seed(seed)).tolist()
        for seed in seeds
    ]


def make_session(model: Path, provider: str, threads: int):
    # Import torch first so its matching CUDA/cuDNN DLLs are visible to ORT.
    import torch  # noqa: F401
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    requested = "CUDAExecutionProvider" if provider == "cuda" else "CPUExecutionProvider"
    if requested not in ort.get_available_providers():
        raise RuntimeError(f"requested provider unavailable: {requested}")
    providers = (
        [requested]
        if provider == "cpu"
        else [(requested, {"use_tf32": "0"}), "CPUExecutionProvider"]
    )
    session = ort.InferenceSession(str(model), sess_options=options, providers=providers)
    if session.get_providers()[0] != requested:
        raise RuntimeError(f"provider fallback: {session.get_providers()}")
    return (
        session,
        ort.__version__,
        torch.__version__,
        session.get_providers(),
        session.get_provider_options(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", choices=("cmi", "mesa"), required=True)
    parser.add_argument("--phase", choices=("validation", "test"), required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--context", type=int, required=True)
    parser.add_argument("--ensembles", type=int, choices=(1, 2), required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--source-cache", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--selection-lock", type=Path, required=True)
    parser.add_argument("--method-lock", type=Path, required=True)
    parser.add_argument("--execution-lock", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--provider", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    protocol = read_json(args.protocol)
    selection = read_json(args.selection_lock)
    method = read_json(args.method_lock)
    execution = read_json(args.execution_lock)
    if selection["selected_candidate"] != args.candidate_id:
        raise RuntimeError("candidate does not match selection lock")
    if method["selected_candidate"] != args.candidate_id:
        raise RuntimeError("candidate does not match method lock")
    if method["method_id"] != METHOD_ID or method["retrieval_method_id"] != RETRIEVAL_ID:
        raise RuntimeError("mobile method identity mismatch")
    if int(method["context_size"]) != args.context or int(method["ensembles"]) != args.ensembles:
        raise RuntimeError("mobile resource setting mismatch")
    protocol_hash = sha256(args.protocol)
    if selection["protocol_sha256"] != protocol_hash or method["protocol_sha256"] != protocol_hash:
        raise RuntimeError("protocol hash mismatch")
    if execution["protocol_sha256"] != protocol_hash:
        raise RuntimeError("execution lock protocol mismatch")
    if execution["portable_method_lock_sha256"] != sha256(args.method_lock):
        raise RuntimeError("execution lock method mismatch")
    if execution["selected_candidate"] != args.candidate_id:
        raise RuntimeError("execution lock candidate mismatch")
    for name, expected in execution["source_sha256"].items():
        if sha256(Path(__file__).resolve().parent / name) != expected:
            raise RuntimeError(f"frozen portable source changed: {name}")
    model_hashes = execution["model_sha256"]
    if sha256(args.model) != model_hashes[args.model.name]:
        raise RuntimeError("frozen ONNX graph changed")
    external = args.model.with_name(args.model.name + ".data")
    if sha256(external) != model_hashes[external.name]:
        raise RuntimeError("frozen ONNX external data changed")
    expected_cache_hash = protocol["cache_sha256"][args.source_cache.name]
    if sha256(args.source_cache) != expected_cache_hash:
        raise RuntimeError("source cache hash mismatch")

    with np.load(args.source_cache, allow_pickle=False) as stored:
        data = {name: stored[name].copy() for name in stored.files}
    train = data["splits"] == "train"
    evaluate = data["splits"] == args.phase
    if set(data["groups"][train]) & set(data["groups"][evaluate]):
        raise RuntimeError("participant leakage")
    x_train = np.asarray(data["X_hybrid"][train], dtype=np.float32)
    x_eval = np.asarray(data["X_hybrid"][evaluate], dtype=np.float32)
    y_train = np.asarray(data["y"][train], dtype=np.float32)

    session_started = time.perf_counter()
    session, ort_version, torch_version, active_providers, provider_options = make_session(
        args.model, args.provider, args.threads
    )
    if ort_version != execution["runtime_versions"]["onnxruntime_gpu"]:
        raise RuntimeError("ONNX Runtime version changed")
    if torch_version != execution["runtime_versions"]["torch"]:
        raise RuntimeError("PyTorch version changed")
    if np.__version__ != execution["runtime_versions"]["numpy"]:
        raise RuntimeError("NumPy version changed")
    if int(session.get_inputs()[0].shape[0]) != int(execution["batch_size"]):
        raise RuntimeError("ONNX batch size changed")
    if args.provider == "cuda" and provider_options["CUDAExecutionProvider"].get("use_tf32") != "0":
        raise RuntimeError("CUDA TF32 must be disabled")
    session_seconds = time.perf_counter() - session_started
    feature_permutations = permutations(x_train.shape[1], args.ensembles)
    permutation_hash = hashlib.sha256(
        np.ascontiguousarray(feature_permutations, dtype="<i8").tobytes()
    ).hexdigest()
    if permutation_hash != execution["feature_permutations_sha256"][args.cohort]:
        raise RuntimeError("feature permutations changed")
    raw_channels: list[np.ndarray] = []
    channel_receipts: list[dict] = []
    started = time.perf_counter()
    for channel in range(2):
        prediction, timing = portable_onnx_predict_target(
            session,
            x_train,
            y_train[:, channel],
            x_eval,
            feature_permutations,
            context_size=args.context,
        )
        raw_channels.append(prediction)
        channel_receipts.append({"channel": channel, **timing})
    raw = np.column_stack(raw_channels)
    prediction = constrain_predictions(raw)
    if prediction.shape != (int(evaluate.sum()), 2) or not np.isfinite(prediction).all():
        raise RuntimeError("prediction shape/coverage mismatch")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.output_dir / f"{args.candidate_id}_portable_{args.cohort}_{args.phase}"
    np.save(stem.with_suffix(".npy"), prediction, allow_pickle=False)
    source_files = (
        "mobile_retrieval.py",
        "portable_runtime.py",
        "portable_onnx_predict.py",
        "run_selected_onnx.py",
    )
    source_hashes = {name: sha256(Path(__file__).resolve().parent / name) for name in source_files}
    model_files = [args.model] + ([external] if external.is_file() else [])
    receipt = {
        "family": "tabdpt_v1_3",
        "variant": "actual_mobile_onnx_fp32",
        "method_id": METHOD_ID,
        "retrieval_method_id": RETRIEVAL_ID,
        "candidate_id": args.candidate_id,
        "context_size": args.context,
        "n_ensembles": args.ensembles,
        "cohort": args.cohort,
        "phase": args.phase,
        "rows": int(evaluate.sum()),
        "batch_size": int(session.get_inputs()[0].shape[0]),
        "requested_provider": args.provider,
        "active_providers": active_providers,
        "provider_options": provider_options,
        "onnxruntime_version": ort_version,
        "torch_version": torch_version,
        "numpy_version": np.__version__,
        "graph_optimization_level": "ORT_ENABLE_ALL(default)",
        "feature_permutations_sha256": permutation_hash,
        "session_seconds": session_seconds,
        "channels": channel_receipts,
        "seconds_total_prediction": time.perf_counter() - started,
        "prediction_sha256": sha256(stem.with_suffix(".npy")),
        "protocol_sha256": protocol_hash,
        "selection_lock_sha256": sha256(args.selection_lock),
        "portable_method_lock_sha256": sha256(args.method_lock),
        "portable_execution_lock_sha256": sha256(args.execution_lock),
        "source_cache_sha256": expected_cache_hash,
        "source_sha256": source_hashes,
        "model_files": [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in model_files
        ],
        "raw_prediction_min_max": [float(raw.min()), float(raw.max())],
        "constrained_rows_changed": int(np.any(raw != prediction, axis=1).sum()),
    }
    stem.with_suffix(".json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
