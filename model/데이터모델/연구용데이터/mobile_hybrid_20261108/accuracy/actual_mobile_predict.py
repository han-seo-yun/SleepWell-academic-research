"""Predict with the frozen deployable mobile math contract on the locked split."""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import sys
import time
from pathlib import Path

for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ[name] = "2"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"

ROOT = Path(r"C:/Users/pyomo/OneDrive/문서/ChatGPT/학술제")
OUT = Path(__file__).resolve().parent
EXPORT = OUT.parent / "mobile_export"
SOURCE = ROOT / "reports/deep_learning_comparison_20261105/private"
WEIGHTS = ROOT / "research_workbench_academic/.models/tabdpt-v1.3/tabdpt1_3.safetensors"
sys.path[:0] = [str(ROOT / "research_workbench_academic/scripts"), str(EXPORT)]

import numpy as np


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", choices=("cmi", "mesa"), required=True)
    parser.add_argument("--phase", choices=("validation", "test"), required=True)
    args = parser.parse_args()
    p, lock, method = read(OUT / "protocol.json"), read(OUT / "selection_lock.json"), read(OUT / "portable_method_lock.json")
    protocol_sha = sha(OUT / "protocol.json")
    if lock["protocol_sha256"] != protocol_sha or method["protocol_sha256"] != protocol_sha:
        raise RuntimeError("protocol lock mismatch")
    if method["selected_candidate"] != lock["selected_candidate"]:
        raise RuntimeError("selected candidate mismatch")
    for relative, expected in method["source_sha256"].items():
        if sha(EXPORT / relative) != expected:
            raise RuntimeError(f"portable source changed: {relative}")
    selected = next(item for item in p["candidates"] if item["id"] == lock["selected_candidate"])
    if selected["context_size"] != method["context_size"] or selected["ensembles"] != method["ensembles"]:
        raise RuntimeError("portable method setting mismatch")
    if args.phase == "test":
        for cohort in p["cohorts"]:
            if not (OUT / "private" / f"{selected['id']}_actual_mobile_{cohort}_validation.json").is_file():
                raise RuntimeError("portable Validation must finish before portable Test")
    cache = SOURCE / f"{args.cohort}.npz"
    if sha(cache) != p["cache_sha256"][cache.name]:
        raise RuntimeError("source cache changed")
    with np.load(cache, allow_pickle=False) as stored:
        data = {name: stored[name].copy() for name in stored.files}
    train, evaluate = data["splits"] == "train", data["splits"] == args.phase
    if set(data["groups"][train]) & set(data["groups"][evaluate]):
        raise RuntimeError("participant leakage")

    import torch
    import tabular_foundation_models as tfm
    from portable_predict import METHOD_ID, portable_predict_fitted

    if METHOD_ID != method["method_id"]:
        raise RuntimeError("portable method identity changed")
    torch.set_num_threads(2)
    channels, channel_info = [], []
    started_all = time.perf_counter()
    for channel in range(2):
        model = None
        try:
            tfm._seed_runtime(p["seed"])
            model = tfm._make_model(
                "tabdpt_v1_3", WEIGHTS, p["seed"], int(selected["ensembles"]), int(selected["context_size"])
            )
            model.fit(data["X_hybrid"][train], data["y"][train, channel])
            started = time.perf_counter()
            prediction = portable_predict_fitted(
                model, data["X_hybrid"][evaluate], context_size=int(selected["context_size"]),
                n_ensembles=int(selected["ensembles"]), seed=p["seed"], batch_size=16,
            )
            channels.append(prediction)
            channel_info.append({"channel": channel, "seconds": time.perf_counter() - started})
        finally:
            if model is not None:
                del model
            gc.collect()
            torch.cuda.empty_cache()
    prediction = tfm.constrain_predictions(np.column_stack(channels))
    if prediction.shape != (int(evaluate.sum()), 2) or not np.isfinite(prediction).all():
        raise RuntimeError("portable prediction shape/coverage mismatch")
    stem = OUT / "private" / f"{selected['id']}_actual_mobile_{args.cohort}_{args.phase}"
    np.save(stem.with_suffix(".npy"), prediction, allow_pickle=False)
    receipt = {
        "family": "tabdpt_v1_3", "variant": "actual_mobile_math_reference_gpu",
        "method_id": METHOD_ID, "candidate_id": selected["id"],
        "context_size": selected["context_size"], "n_ensembles": selected["ensembles"],
        "cohort": args.cohort, "phase": args.phase, "rows": int(evaluate.sum()),
        "channels": channel_info, "seconds_total": time.perf_counter() - started_all,
        "prediction_sha256": sha(stem.with_suffix(".npy")), "protocol_sha256": protocol_sha,
        "selection_lock_sha256": sha(OUT / "selection_lock.json"),
        "portable_method_lock_sha256": sha(OUT / "portable_method_lock.json"),
        "source_sha256": method["source_sha256"],
        "runtime_scope": "GPU reference for frozen Android math; actual ONNX parity and A52s timing are separate checks",
    }
    write(stem.with_suffix(".json"), receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
