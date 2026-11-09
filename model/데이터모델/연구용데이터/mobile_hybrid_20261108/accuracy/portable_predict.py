"""Generate the selected candidate with the deterministic mobile retrieval rule."""
from __future__ import annotations

import argparse
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
    protocol_path, lock_path = OUT / "protocol.json", OUT / "selection_lock.json"
    p, lock = read(protocol_path), read(lock_path)
    if lock["protocol_sha256"] != sha(protocol_path):
        raise RuntimeError("selection lock protocol mismatch")
    selected = next(item for item in p["candidates"] if item["id"] == lock["selected_candidate"])
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
    from mobile_retrieval import bind_mobile_retrieval

    method_id = "mobile_l2_f32store_f64accum_index_tie_v1"

    original_make_model = tfm._make_model

    def portable_make_model(*model_args, **model_kwargs):
        return bind_mobile_retrieval(original_make_model(*model_args, **model_kwargs))

    tfm._make_model = portable_make_model
    tfm.ENSEMBLES = int(selected["ensembles"])
    torch.set_num_threads(2)
    started = time.perf_counter()
    result = tfm.fit_predict(
        "tabdpt_v1_3", data["X_hybrid"][train], data["y"][train], data["X_hybrid"][evaluate],
        WEIGHTS, seed=p["seed"], n_ensembles=int(selected["ensembles"]),
        context_size=int(selected["context_size"]),
    )
    elapsed = time.perf_counter() - started
    prediction = np.asarray(result["predictions"], dtype=np.float64)
    if prediction.shape != (int(evaluate.sum()), 2) or not np.isfinite(prediction).all():
        raise RuntimeError("prediction shape/coverage mismatch")
    stem = OUT / "private" / f"{selected['id']}_portable_{args.cohort}_{args.phase}"
    np.save(stem.with_suffix(".npy"), prediction, allow_pickle=False)
    receipt = {
        **result["metadata"], "candidate_id": selected["id"],
        "variant": "deterministic_retrieval_pytorch_reference_not_android_runtime",
        "retrieval_method_id": method_id, "retrieval_source_sha256": sha(EXPORT / "mobile_retrieval.py"),
        "cohort": args.cohort, "phase": args.phase, "seconds_total": elapsed,
        "prediction_sha256": sha(stem.with_suffix(".npy")), "protocol_sha256": sha(protocol_path),
        "protocol_amendment_sha256": sha(OUT / "protocol_amendment.json"),
        "cache_sha256": p["cache_sha256"][cache.name],
        "evaluation_rows": int(evaluate.sum()),
        "timing_scope": "desktop RTX GPU with portable retrieval rule; not Galaxy A52s latency",
    }
    write(stem.with_suffix(".json"), receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
