"""Pre-specified causal replay of online residual personalization.

The frozen robust-v2 forecasts already contain historical sleep features.  This
experiment evaluates an additional online residual sidecar; it is not a
comparison with an unpersonalized model and it does not refit model weights.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import platform
import shutil
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web-app"
sys.path.insert(0, str(WEB))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adaptive_personalization import POLICY_VERSION, compute_adjustment
from night_core import constrain
from robust_features import DATA_V2, HYBRID_V2


SEED = 20261025
COHORTS = ("cmi", "mesa", "samsung")
ARMS = {"data": list(DATA_V2), "hybrid": list(HYBRID_V2)}
TARGETS = ["bed_offset", "wake_offset"]


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def project(values):
    return np.asarray([constrain(*row) for row in values], dtype=float)


def row_errors(actual, predicted):
    absolute = np.abs(actual - predicted)
    return np.column_stack([absolute[:, 0], absolute[:, 1], absolute.mean(axis=1)])


def metrics(actual, predicted, subjects, macro):
    if len(actual) == 0:
        return None
    values = pd.DataFrame(row_errors(actual, predicted), columns=["onset_mae_min", "wake_mae_min", "joint_mae_min"])
    values["subject"] = np.asarray(subjects)
    average = values.groupby("subject").mean().mean() if macro else values.drop(columns="subject").mean()
    return {**{key: float(value) for key, value in average.items()},
            "n_nights": int(len(values)), "n_people": int(values.subject.nunique()),
            "averaging": "equal participant" if macro else "equal night"}


def paired_interval(actual, base, corrected, subjects, temporal):
    if len(actual) == 0:
        return None
    delta = row_errors(actual, base) - row_errors(actual, corrected)
    rng = np.random.default_rng(SEED)
    if temporal:
        n = len(delta)
        draws = []
        for _ in range(2000):
            starts = rng.integers(0, n, size=int(np.ceil(n / 7)))
            indexes = np.concatenate([(start + np.arange(7)) % n for start in starts])[:n]
            draws.append(delta[indexes].mean(axis=0))
        observed = delta.mean(axis=0)
        method = "paired circular 7-night block bootstrap; one participant"
    else:
        grouped = pd.DataFrame(delta).assign(subject=np.asarray(subjects)).groupby("subject").mean().to_numpy()
        draws = [grouped[rng.integers(0, len(grouped), len(grouped))].mean(axis=0) for _ in range(2000)]
        observed = grouped.mean(axis=0)
        method = "paired participant cluster bootstrap"
    interval = np.quantile(draws, [0.025, 0.975], axis=0)
    return {"method": method, "replicates": 2000,
            "positive_means": "lower error for corrected sidecar",
            "metrics": {name: {"error_reduction_min": float(observed[index]),
                               "ci95": interval[:, index].tolist()}
                        for index, name in enumerate(("onset", "wake", "joint"))}}


def replay(frame, predictions, minimum_history):
    corrected = np.empty_like(predictions)
    diagnostics = [None] * len(frame)
    grouping = ["subject_id", "split"] if frame.cohort.iloc[0] == "samsung" else ["subject_id"]
    for _, group in frame.assign(position=np.arange(len(frame))).groupby(grouping, sort=True):
        history = []
        for row in group.sort_values("cutoff").itertuples(index=False):
            position = row.position
            adjustment = compute_adjustment(history, row.cutoff, minimum_history=minimum_history)
            corrected[position] = project([[
                predictions[position, 0] + adjustment["onset_correction_min"],
                predictions[position, 1] + adjustment["wake_correction_min"],
            ]])[0]
            diagnostics[position] = adjustment
            history.append({
                "night_id": str(row.row_id),
                "available_at": pd.Timestamp(row.observed_at).isoformat(),
                "outcome_at": pd.Timestamp(row.wake).isoformat(),
                "onset_residual_min": float(row.bed_offset - predictions[position, 0]),
                "wake_residual_min": float(row.wake_offset - predictions[position, 1]),
            })
    return corrected, pd.DataFrame(diagnostics)


def _stratum_metrics(frame, actual, base, corrected, mask, macro):
    mask = np.asarray(mask, dtype=bool)
    return {
        "n_nights": int(mask.sum()),
        "n_people": int(frame.loc[mask, "subject_id"].nunique()),
        "base_metrics": metrics(actual[mask], base[mask], frame.loc[mask, "subject_id"], macro),
        "corrected_metrics": metrics(actual[mask], corrected[mask], frame.loc[mask, "subject_id"], macro),
        "paired_improvement": paired_interval(
            actual[mask], base[mask], corrected[mask], frame.loc[mask, "subject_id"], not macro
        ),
    }


def summarize_arm(frame, features, actual, base, corrected, diagnostics, cohort):
    test = frame.split.eq("test").to_numpy()
    macro = cohort != "samsung"
    active = diagnostics.active.fillna(False).to_numpy(bool)
    eligible_count = diagnostics.eligible_count.fillna(0).to_numpy(int)
    gap = diagnostics.last_outcome_gap_days.to_numpy(float)
    train = frame.split.eq("train").to_numpy()
    center = np.median(actual[train], axis=0)
    scale = np.maximum(30.0, 1.4826 * np.median(np.abs(actual[train] - center), axis=0))
    rare = np.max(np.abs(actual - center) / scale, axis=1) > 3.5
    missing = features.isna().sum(axis=1).to_numpy()
    history_masks = {
        "0-3": eligible_count <= 3,
        "4-13": (eligible_count >= 4) & (eligible_count <= 13),
        "14-27": (eligible_count >= 14) & (eligible_count <= 27),
        "28+": eligible_count >= 28,
    }
    gap_masks = {
        "no_history": np.isnan(gap), "0-2_days": gap <= 2,
        "2-7_days": (gap > 2) & (gap <= 7), "over_7_days": gap > 7,
    }
    qc_masks = {"statistically_rare": rare, "other": ~rare}
    missing_masks = {"no_missing_predictors": missing == 0, "has_missing_predictors": missing > 0}
    all_rows = _stratum_metrics(frame, actual, base, corrected, test, macro)
    active_rows = _stratum_metrics(frame, actual, base, corrected, test & active, macro)
    return {
        "base_arm_label": "frozen robust-v2 model; already includes causal personal-history features",
        "n_people": int(frame.loc[test, "subject_id"].nunique()),
        "n_nights": int(test.sum()),
        "eligible_personalized_nights": int((test & active).sum()),
        "coverage": float((test & active).sum() / test.sum()) if test.sum() else 0.0,
        "all_test_rows_including_zero_correction": all_rows,
        "active_subset_same_rows": active_rows,
        "history_strata": {name: _stratum_metrics(frame, actual, base, corrected, test & mask, macro)
                            for name, mask in history_masks.items()},
        "gap_strata": {name: _stratum_metrics(frame, actual, base, corrected, test & mask, macro)
                        for name, mask in gap_masks.items()},
        "qc_strata": {name: _stratum_metrics(frame, actual, base, corrected, test & mask, macro)
                       for name, mask in qc_masks.items()},
        "missingness_strata": {name: _stratum_metrics(frame, actual, base, corrected, test & mask, macro)
                               for name, mask in missing_masks.items()},
        "qc_definition": {"center": center.tolist(), "scale": scale.tolist(), "threshold": 3.5,
                          "meaning": "training-target rarity only; not proven sensor error; no rows removed"},
    }


def run_cohort(input_dir, output_dir, cohort):
    output_dir.mkdir()
    frame = pd.read_parquet(input_dir / "episodes.parquet").copy()
    features = pd.read_parquet(input_dir / "features.parquet").copy()
    states = pd.read_parquet(input_dir / "states.parquet").copy()
    if not (frame.row_id.tolist() == features.row_id.tolist() == states.row_id.tolist()):
        raise ValueError("row identity mismatch")
    frame["cutoff"] = pd.to_datetime(frame.cutoff, utc=True)
    frame["observed_at"] = pd.to_datetime(frame.observed_at, utc=True)
    actual = frame[TARGETS].to_numpy(float)
    cohort_summary = {"notes": [
        "Frozen data/hybrid models already contain historical sleep features.",
        "This evaluates only an additional online residual sidecar.",
    ]}
    private = frame[["row_id", "subject_id", "split", "cutoff", "observed_at", *TARGETS]].copy()
    for arm, names in ARMS.items():
        model = joblib.load(input_dir / f"{arm}.joblib")
        base = project(model.predict(features[names].to_numpy(float)))
        corrected, diagnostics = replay(frame, base, minimum_history=14)
        exploratory, exploratory_diagnostics = replay(frame, base, minimum_history=3)
        cohort_summary[arm] = summarize_arm(frame, features[names], actual, base, corrected, diagnostics, cohort)
        cohort_summary[arm]["exploratory_min3_not_promoted"] = summarize_arm(
            frame, features[names], actual, base, exploratory, exploratory_diagnostics, cohort
        )
        for index, name in enumerate(("onset", "wake")):
            private[f"{arm}_base_{name}"] = base[:, index]
            private[f"{arm}_corrected_{name}"] = corrected[:, index]
        for column in diagnostics:
            if column not in {"interpretation", "uncertainty_reason"}:
                private[f"{arm}_{column}"] = diagnostics[column]
    recent = project(features[["median14_bed", "median14_wake"]].to_numpy(float))
    test = frame.split.eq("test").to_numpy()
    cohort_summary["naive_recent_baseline"] = metrics(
        actual[test], recent[test], frame.loc[test, "subject_id"], cohort != "samsung"
    )
    private.to_parquet(output_dir / "private_row_replay.parquet", index=False)
    dump(output_dir / "summary.json", cohort_summary)
    return cohort_summary


def invariance_checks():
    cutoff = pd.Timestamp("2026-04-01T20:00:00+09:00")
    history = [{"night_id": str(index), "available_at": (cutoff - pd.Timedelta(days=index + 1)).isoformat(),
                "onset_residual_min": float(index), "wake_residual_min": float(-index)} for index in range(14)]
    expected = compute_adjustment(history, cutoff)
    shuffled = compute_adjustment(list(reversed(history)), cutoff)
    future = compute_adjustment(history + [{"night_id": "future", "available_at": cutoff.isoformat(),
                                            "onset_residual_min": 1e9, "wake_residual_min": 1e9}], cutoff)
    if expected != shuffled or expected != future:
        raise AssertionError("future/order invariance failed")
    return {"history_order_invariance": True, "current_future_outcome_invariance": True}


def public_artifact(private_artifact):
    """Remove the single-person auxiliary cohort from distributable output."""
    result = copy.deepcopy(private_artifact)
    result["cohorts"] = {name: private_artifact["cohorts"][name] for name in ("cmi", "mesa")}
    result["privacy"] = {
        "personal_data_included": False,
        "note": "private auxiliary cohort excluded from distributable",
        "excluded_cohorts": ["samsung"],
    }
    result["limitations"] = [
        item for item in private_artifact["limitations"] if "Samsung" not in item
    ] + ["Samsung single-person auxiliary results remain only in the private research run."]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    inputs = {f"{cohort}/{name}": sha(args.input / cohort / name)
              for cohort in COHORTS for name in ("episodes.parquet", "features.parquet", "states.parquet",
                                                  "data.joblib", "hybrid.joblib")}
    protocol = {
        "version": "personalization-online-residual-20261025-v1", "seed": SEED,
        "written_before_outcome_scoring": True,
        "target": "next main-sleep onset and final wake offsets from local 20:00; unchanged rows",
        "base": "frozen robust-v2 data/hybrid model; already contains causal history features",
        "policy": {"policy_version": POLICY_VERSION, "residual_sign": "actual-frozen_prediction",
                   "formula_basis": "pre-specified engineering policy proposed for evaluation; not a literature-derived physiological constant",
                   "half_life_days": 14, "shrinkage_lambda": 14, "minimum_history": 14,
                   "estimator": "recency-weighted arithmetic mean residual",
                   "availability": "available_at strictly before current cutoff; outcome_at defaults to available_at",
                   "recency_clock": "outcome_at (wake/event time), never late submission time",
                   "same_night_revision": "latest eligible available_at",
                   "projection": "existing onset/wake support after correction"},
        "exploratory_policy": {"minimum_history": 3, "service_promoted": False},
        "evaluation": "test rows in original split; online sequential outcomes from earlier nights only",
        "test_status": "previously exposed reused Test; adaptive exploratory reanalysis, not fresh confirmation",
        "split_boundary": "Samsung resets history at each split; CMI/MESA participants are split-disjoint",
        "timestamp_assumption": "retrospective observed_at equals supplied outcome availability, often wake time; this is not prospective audit proof",
        "outlier_policy": "no target removed; train-only MAD rarity is descriptive",
        "service_promotion": False, "input_hashes": inputs,
        "software": {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__},
    }
    dump(args.output / "protocol.json", protocol)
    snapshot = args.output / "code_snapshot"
    snapshot.mkdir()
    for source in (Path(__file__), WEB / "adaptive_personalization.py"):
        shutil.copy2(source, snapshot / source.name)
    checks = invariance_checks()
    summaries = {cohort: run_cohort(args.input / cohort, args.output / cohort, cohort) for cohort in COHORTS}
    for relative, expected in inputs.items():
        if sha(args.input / relative) != expected:
            raise ValueError("input changed during run")
    artifact = {
        "schema_version": 1, "artifact_type": "sleepwell_personalization_validation",
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "evaluation_status": {"reused_test": True, "fresh_external_confirmation": False,
                              "estimand": "online temporal update using only earlier resolved outcomes"},
        "prediction_target": "next main-sleep onset and final wake clock time; not sleep quality or benefit",
        "correction_policy": {**protocol["policy"], "service_promoted": False,
                              "uncertainty": "unknown; no validated prediction interval"},
        "cohorts": summaries,
        "limitations": [
            "Retrospective evaluation with previously exposed test sets.",
            "Observed_at is an assumed availability timestamp, not a prospective audit log.",
            "CMI and MESA labels differ; MESA uses an ACT-derived weak label.",
            "Samsung is one person and is auxiliary only.",
            "No causal evidence that a recommendation improves sleep regularity or next-day function.",
            "Fixed Two-Process constants are not individual physiological estimates.",
        ],
        "provenance": {"protocol_sha256": sha(args.output / "protocol.json"),
                       "code_sha256": {p.name: sha(p) for p in snapshot.iterdir()},
                       "input_hashes": inputs, "invariance_checks": checks},
    }
    private_path = args.output / "personalization_validation_v1.json"
    dump(private_path, artifact)
    public = public_artifact(artifact)
    public["provenance"]["source_private_aggregate_sha256"] = sha(private_path)
    dump(args.output / "personalization_validation_public_v1.json", public)
    dump(args.output / "verification.json", {"status": "pass", "checks": checks,
                                             "all_inputs_unchanged": True,
                                             "all_target_rows_retained": True,
                                             "service_promoted": False})
    print(json.dumps({cohort: {arm: summaries[cohort][arm]["all_test_rows_including_zero_correction"]
                               ["paired_improvement"]["metrics"]["joint"]
                               for arm in ARMS} for cohort in COHORTS}, ensure_ascii=False))


if __name__ == "__main__":
    main()
