"""Locked lightweight TabDPT accuracy experiment on a person-disjoint split.

Stages: preflight, Validation predictions, select, selected Test predictions,
and finalize. Production artifacts are read-only. Historical Test reuse means
the Test result is descriptive, not a fresh independent evaluation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

for _name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ[_name] = "2"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"

ROOT = Path(r"C:/Users/pyomo/OneDrive/문서/ChatGPT/학술제")
OUT = Path(__file__).resolve().parent
PRIVATE = OUT / "private"
PROTOCOL = OUT / "protocol.json"
SOURCE_CACHE = ROOT / "reports/deep_learning_comparison_20261105/private"
BASELINE = ROOT / "reports/equal_budget_hybrid9_20261107"
SCRIPTS = ROOT / "research_workbench_academic/scripts"
WEIGHTS = ROOT / "research_workbench_academic/.models/tabdpt-v1.3/tabdpt1_3.safetensors"
sys.path.insert(0, str(SCRIPTS))

import numpy as np


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def protocol() -> dict[str, Any]:
    return read_json(PROTOCOL)


def verify_frozen(p: dict[str, Any]) -> None:
    for relative, expected in p["frozen_sha256"].items():
        path = ROOT / relative
        if not path.is_file() or sha256(path) != expected:
            raise RuntimeError(f"frozen artifact changed: {relative}")


def load_cohort(cohort: str, p: dict[str, Any]) -> dict[str, np.ndarray]:
    path = SOURCE_CACHE / f"{cohort}.npz"
    if sha256(path) != p["cache_sha256"][path.name]:
        raise RuntimeError(f"source cache changed: {path.name}")
    with np.load(path, allow_pickle=False) as stored:
        return {name: stored[name].copy() for name in stored.files}


def person_sets(data: dict[str, np.ndarray]) -> dict[str, set[str]]:
    return {
        split: set(map(str, data["groups"][data["splits"] == split].tolist()))
        for split in ("train", "validation", "test")
    }


def verify_split(data: dict[str, np.ndarray], cohort: str, p: dict[str, Any]) -> dict[str, Any]:
    sets = person_sets(data)
    for left, right in (("train", "validation"), ("train", "test"), ("validation", "test")):
        if sets[left] & sets[right]:
            raise RuntimeError(f"participant leakage in {cohort}: {left}/{right}")
    observed = {
        split: {"rows": int(np.sum(data["splits"] == split)), "participants": len(sets[split])}
        for split in sets
    }
    if observed != p["split_counts"][cohort]:
        raise RuntimeError(f"split counts changed for {cohort}: {observed}")
    return observed


def export_context(cohort: str, data: dict[str, np.ndarray], p: dict[str, Any]) -> dict[str, Any]:
    train = data["splits"] == "train"
    path = PRIVATE / f"context_{cohort}.npz"
    np.savez_compressed(
        path,
        X_train=data["X_hybrid"][train].astype(np.float32),
        y_train=data["y"][train].astype(np.float32),
        row_ids=data["row_ids"][train],
        groups=data["groups"][train],
    )
    contract = p["mobile_export_contract"][cohort]
    return {
        "path": str(path), "sha256": sha256(path), "rows": int(train.sum()),
        "transformed_features": int(data["X_hybrid"].shape[1]),
        "raw_features": int(data["X_hybrid_raw"].shape[1]),
        "preprocessor_path": contract["preprocessor_path"],
        "preprocessor_sha256": contract["preprocessor_sha256"],
        "retrieval": "exact squared L2 on transformed X_train; ascending training-row index resolves equal distances",
    }


def preflight() -> None:
    p = protocol()
    verify_frozen(p)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    splits, contexts = {}, {}
    for cohort in p["cohorts"]:
        data = load_cohort(cohort, p)
        splits[cohort] = verify_split(data, cohort, p)
        contexts[cohort] = export_context(cohort, data, p)
    result = {
        "status": "pass", "protocol_sha256": sha256(PROTOCOL),
        "participant_overlap": 0, "splits": splits, "contexts": contexts,
        "test_status": "historically reused; descriptive only",
    }
    write_json(OUT / "preflight.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def candidate_by_id(p: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    matches = [item for item in p["candidates"] if item["id"] == candidate_id]
    if len(matches) != 1:
        raise ValueError(f"unknown candidate: {candidate_id}")
    return matches[0]


def prediction_stem(candidate_id: str, cohort: str, phase: str) -> Path:
    return PRIVATE / f"{candidate_id}_{cohort}_{phase}"


def predict(candidate_id: str, cohort: str, phase: str) -> None:
    p = protocol()
    verify_frozen(p)
    candidate = candidate_by_id(p, candidate_id)
    if cohort not in p["cohorts"] or phase not in ("validation", "test"):
        raise ValueError("invalid cohort or phase")
    if phase == "test":
        lock_path = OUT / "selection_lock.json"
        if not lock_path.is_file():
            raise RuntimeError("Test is closed until Validation selection is locked")
        lock = read_json(lock_path)
        if lock["protocol_sha256"] != sha256(PROTOCOL) or lock["selected_candidate"] != candidate_id:
            raise RuntimeError("only the Validation-selected candidate may access Test")
    data = load_cohort(cohort, p)
    verify_split(data, cohort, p)
    train, evaluate = data["splits"] == "train", data["splits"] == phase
    if set(data["groups"][train]) & set(data["groups"][evaluate]):
        raise RuntimeError("participant leakage")

    import torch
    import tabular_foundation_models as tfm

    torch.set_num_threads(2)
    tfm.ENSEMBLES = int(candidate["ensembles"])
    started = time.perf_counter()
    result = tfm.fit_predict(
        "tabdpt_v1_3", data["X_hybrid"][train], data["y"][train],
        data["X_hybrid"][evaluate], WEIGHTS, seed=p["seed"],
        n_ensembles=int(candidate["ensembles"]), context_size=int(candidate["context_size"]),
    )
    elapsed = time.perf_counter() - started
    prediction = np.asarray(result["predictions"], dtype=np.float64)
    if prediction.shape != (int(evaluate.sum()), 2) or not np.isfinite(prediction).all():
        raise RuntimeError("prediction shape/coverage failure")
    stem = prediction_stem(candidate_id, cohort, phase)
    np.save(stem.with_suffix(".npy"), prediction, allow_pickle=False)
    receipt = {
        **result["metadata"], "candidate_id": candidate_id, "cohort": cohort, "phase": phase,
        "seconds_total": elapsed, "prediction_sha256": sha256(stem.with_suffix(".npy")),
        "protocol_sha256": sha256(PROTOCOL), "cache_sha256": p["cache_sha256"][f"{cohort}.npz"],
        "evaluation_rows": int(evaluate.sum()),
        "evaluation_participants": len(set(map(str, data["groups"][evaluate].tolist()))),
        "timing_scope": "desktop RTX GPU exploratory; not Galaxy A52s latency",
    }
    write_json(stem.with_suffix(".json"), receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


def load_prediction(path: Path, expected_rows: int, expected_sha: str | None = None) -> np.ndarray:
    if not path.is_file() or (expected_sha is not None and sha256(path) != expected_sha):
        raise RuntimeError(f"missing or changed prediction: {path}")
    value = np.load(path, allow_pickle=False)
    if value.shape != (expected_rows, 2) or not np.isfinite(value).all():
        raise RuntimeError(f"invalid prediction: {path}")
    return np.asarray(value, dtype=float)


def score_prediction(cohort: str, phase: str, model: str, prediction: np.ndarray, data: dict[str, Any]):
    from compare_deep_tabular import evaluate_cell
    return evaluate_cell(cohort, "hybrid", data, phase, prediction, model)


def load_dataset() -> dict[str, Any]:
    from prepare_deep_comparison_data import load_private_dataset
    return load_private_dataset(SOURCE_CACHE)["cohorts"]


def baseline_prediction(model: str, cohort: str, phase: str, rows: int, p: dict[str, Any]) -> np.ndarray:
    trial = p["baselines"][model]["trial"]
    path = BASELINE / "private" / f"{model}_{cohort}_{trial}_{phase}.npy"
    expected = p["baselines"][model]["prediction_sha256"][f"{cohort}_{phase}"]
    return load_prediction(path, rows, expected)


def validate_candidate_prediction(candidate_id: str, cohort: str, phase: str, rows: int) -> np.ndarray:
    stem = prediction_stem(candidate_id, cohort, phase)
    receipt = read_json(stem.with_suffix(".json"))
    if receipt["protocol_sha256"] != sha256(PROTOCOL):
        raise RuntimeError("candidate prediction belongs to another protocol")
    return load_prediction(stem.with_suffix(".npy"), rows, receipt["prediction_sha256"])


def select() -> None:
    p = protocol()
    verify_frozen(p)
    data = load_dataset()
    rows, scores = [], {}
    for candidate in p["candidates"]:
        values = []
        for cohort in p["cohorts"]:
            count = int(np.sum(data[cohort]["splits"] == "validation"))
            pred = validate_candidate_prediction(candidate["id"], cohort, "validation", count)
            cell, _ = score_prediction(cohort, "validation", candidate["id"], pred, data[cohort])
            rows.append({"model": candidate["id"], **cell})
            values.append(cell["primary"]["participant_macro_joint_mae_min"])
        scores[candidate["id"]] = float(np.mean(values))
    for display, source in (("tabdpt_full_e4", "tabdpt_v1_3"), ("hgb", "hgb")):
        values = []
        for cohort in p["cohorts"]:
            count = int(np.sum(data[cohort]["splits"] == "validation"))
            pred = baseline_prediction(source, cohort, "validation", count, p)
            cell, _ = score_prediction(cohort, "validation", display, pred, data[cohort])
            rows.append({"model": display, **cell})
            values.append(cell["primary"]["participant_macro_joint_mae_min"])
        scores[display] = float(np.mean(values))
    order = {item["id"]: index for index, item in enumerate(p["candidates"])}
    selected = min(order, key=lambda key: (scores[key], order[key]))
    write_json(OUT / "validation.json", {"cells": rows, "aggregate_scores": scores})
    lock = {
        "selected_candidate": selected, "selection_rule": p["selection_rule"],
        "candidate_validation_scores": {key: scores[key] for key in order},
        "protocol_sha256": sha256(PROTOCOL), "validation_sha256": sha256(OUT / "validation.json"),
        "test_evaluated_at_lock": False,
    }
    write_json(OUT / "selection_lock.json", lock)
    print(json.dumps(lock, ensure_ascii=False, indent=2))


def person_errors(records: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[list[float]]] = defaultdict(list)
    for row in records:
        grouped[str(row["participant_id"])].append([float(row["bed_error"]), float(row["wake_error"])])
    return {
        person: {"joint_mae": float(a.mean()), "onset_mae": float(a[:, 0].mean()), "wake_mae": float(a[:, 1].mean())}
        for person, values in grouped.items() for a in [np.asarray(values, dtype=float)]
    }


def raw_person_errors(actual: np.ndarray, predicted: np.ndarray, groups: np.ndarray) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, person in enumerate(groups):
        grouped[str(person)].append(index)
    absolute = np.abs(actual - predicted)
    return {
        person: {"joint_mae": float(absolute[idx].mean()), "onset_mae": float(absolute[idx, 0].mean()), "wake_mae": float(absolute[idx, 1].mean())}
        for person, idx in grouped.items()
    }


def paired_bootstrap(by_model, reference: str, candidate: str, repeats: int, seed: int):
    rng, output = np.random.default_rng(seed), []
    for scope in ("corrected", "raw"):
        for metric in ("joint_mae", "onset_mae", "wake_mae"):
            deltas = []
            for cohort in ("cmi", "mesa"):
                left, right = by_model[reference][cohort][scope], by_model[candidate][cohort][scope]
                if set(left) != set(right):
                    raise RuntimeError("paired participants differ")
                deltas.append(np.asarray([right[p][metric] - left[p][metric] for p in sorted(left)]))
            draws = np.zeros(repeats)
            for values in deltas:
                indices = rng.integers(0, len(values), size=(repeats, len(values)))
                draws += values[indices].mean(axis=1) / len(deltas)
            bounds = np.quantile(draws, [0.025, 0.975])
            output.append({
                "candidate": candidate, "reference": reference, "scope": scope, "metric": metric,
                "delta_min": float(np.mean([v.mean() for v in deltas])),
                "ci_low_min": float(bounds[0]), "ci_high_min": float(bounds[1]),
                "interpretation": "negative favors candidate", "bootstrap_repeats": repeats,
            })
    return output


def finalize() -> None:
    p = protocol()
    verify_frozen(p)
    lock = read_json(OUT / "selection_lock.json")
    if lock["protocol_sha256"] != sha256(PROTOCOL):
        raise RuntimeError("selection lock protocol mismatch")
    selected, data = lock["selected_candidate"], load_dataset()
    models = ("tabdpt_full_e4", "hgb", selected)
    all_errors, cells = {model: {} for model in models}, []
    for cohort in p["cohorts"]:
        mask, count = data[cohort]["splits"] == "test", int(np.sum(data[cohort]["splits"] == "test"))
        for model in models:
            if model == selected:
                pred = validate_candidate_prediction(selected, cohort, "test", count)
            else:
                source = "tabdpt_v1_3" if model == "tabdpt_full_e4" else "hgb"
                pred = baseline_prediction(source, cohort, "test", count, p)
            cell, records = score_prediction(cohort, "test", model, pred, data[cohort])
            cells.append({"model": model, **cell})
            all_errors[model][cohort] = {
                "corrected": person_errors(records),
                "raw": raw_person_errors(data[cohort]["y"][mask], pred, data[cohort]["groups"][mask]),
            }
    aggregate = {
        model: float(np.mean([c["primary"]["participant_macro_joint_mae_min"] for c in cells if c["model"] == model]))
        for model in models
    }
    intervals = []
    for offset, candidate in enumerate(("hgb", selected)):
        intervals.extend(paired_bootstrap(all_errors, "tabdpt_full_e4", candidate, p["bootstrap"]["repeats"], p["bootstrap"]["seed"] + offset))
    result = {
        "cells": cells, "aggregate_corrected_participant_macro_joint_mae_min": aggregate,
        "paired_participant_bootstrap": intervals, "historical_test_reuse": True,
        "decision_threshold": None,
        "decision_note": "No automatic deployment decision; user reviews accuracy and device-resource results.",
    }
    write_json(OUT / "test.json", result)
    complete = {
        "status": "complete", "protocol_sha256": sha256(PROTOCOL),
        "selection_lock_sha256": sha256(OUT / "selection_lock.json"),
        "test_sha256": sha256(OUT / "test.json"), "selected_candidate": selected,
    }
    write_json(OUT / "complete.json", complete)
    print(json.dumps({**complete, "aggregate": aggregate}, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    pred = sub.add_parser("predict")
    pred.add_argument("--candidate", required=True)
    pred.add_argument("--cohort", choices=("cmi", "mesa"), required=True)
    pred.add_argument("--phase", choices=("validation", "test"), required=True)
    sub.add_parser("select")
    sub.add_parser("finalize")
    args = parser.parse_args()
    if args.command == "preflight": preflight()
    elif args.command == "predict": predict(args.candidate, args.cohort, args.phase)
    elif args.command == "select": select()
    else: finalize()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
