"""Supplemental participant-by-target-by-period validity profiles.

This is an exploratory reporting analysis over frozen predictions.  It neither
refits models nor changes the main full-validity protocol or artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from full_validity_experiments import block_skill_ci


COHORTS = ("cmi", "mesa")
MODELS = ("physical", "data", "hybrid")
TARGETS = ("onset", "wake", "joint")
SEED = 20261025


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dump(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _errors(frame: pd.DataFrame, prefix: str) -> dict[str, np.ndarray]:
    onset = np.abs(frame["bed_offset"].to_numpy(float) - frame[f"{prefix}_onset"].to_numpy(float))
    wake = np.abs(frame["wake_offset"].to_numpy(float) - frame[f"{prefix}_wake"].to_numpy(float))
    return {"onset": onset, "wake": wake, "joint": (onset + wake) / 2}


def cohort_profiles(frame: pd.DataFrame, cohort: str) -> tuple[pd.DataFrame, list[dict]]:
    required = {"subject_id", "cutoff", "bed_offset", "wake_offset", "mean14_onset", "mean14_wake"}
    required.update(f"{model}_{target}" for model in MODELS for target in ("onset", "wake"))
    if not required.issubset(frame.columns) or frame.empty:
        raise ValueError(f"{cohort}: missing frozen prediction fields")
    if frame[["subject_id", "cutoff"]].duplicated().any():
        raise ValueError(f"{cohort}: duplicate participant cutoff")
    dates = pd.to_datetime(frame["cutoff"])
    if dates.isna().any():
        raise ValueError(f"{cohort}: invalid cutoff")
    baseline = _errors(frame, "mean14")
    candidates = {model: _errors(frame, model) for model in MODELS}
    records: list[dict] = []
    positioned = frame.assign(_position=np.arange(len(frame)))
    for subject, group in positioned.groupby("subject_id"):
        group = group.sort_values("cutoff")
        group_dates = pd.to_datetime(group["cutoff"])
        window_ids = ((group_dates - group_dates.min()).dt.total_seconds() / 86400 // 14).astype(int)
        for window, part in group.groupby(window_ids):
            indices = part["_position"].to_numpy(int)
            for target in TARGETS:
                base_values = baseline[target][indices]
                base_mae = float(base_values.mean())
                supported = len(indices) >= 4 and base_mae >= 1
                for model in MODELS:
                    model_values = candidates[model][target][indices]
                    model_mae = float(model_values.mean())
                    ci = block_skill_ci(model_values, base_values, part["cutoff"]) if supported else None
                    records.append({
                        "cohort": cohort, "subject_id": subject, "window": int(window),
                        "target": target, "model": model, "n_nights": len(indices),
                        "baseline_mae_min": base_mae, "model_mae_min": model_mae,
                        "skill": float(1 - model_mae / base_mae) if supported else None,
                        "skill_ci_low": ci[0] if ci else None, "skill_ci_high": ci[1] if ci else None,
                        "status": "exploratory_short_series" if supported else "insufficient_nights_or_small_denominator",
                    })
    private = pd.DataFrame(records)
    summary = []
    for (target, model), group in private.groupby(["target", "model"], sort=True):
        valid = group.loc[group["skill"].notna()].copy()
        if len(valid):
            q25, q75 = valid["skill"].quantile([.25, .75]).tolist()
            person_means = valid.groupby("subject_id")["skill"].mean()
            values = {
                "median_participant_window_skill": float(valid["skill"].median()),
                "q25_skill": float(q25), "q75_skill": float(q75),
                "positive_participant_window_fraction": float(valid["skill"].gt(0).mean()),
                "equal_participant_mean_skill": float(person_means.mean()),
            }
        else:
            values = {key: None for key in ("median_participant_window_skill", "q25_skill", "q75_skill",
                                             "positive_participant_window_fraction", "equal_participant_mean_skill")}
        summary.append({
            "cohort": cohort, "target": target, "model": model,
            "n_windows": int(len(group)), "eligible_windows": int(len(valid)),
            "n_people": int(valid["subject_id"].nunique()), **values,
        })
    if len(summary) != len(TARGETS) * len(MODELS):
        raise ValueError(f"{cohort}: incomplete target-model summary")
    return private, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError("Use a new empty supplemental output directory")
    args.output.mkdir(parents=True, exist_ok=True)
    inputs = {cohort: args.run / f"{cohort}_predictions_private.parquet" for cohort in COHORTS}
    if any(not path.is_file() for path in inputs.values()):
        raise FileNotFoundError("Frozen prediction input missing")

    # Freeze the endpoint protocol before reading or calculating any prediction error.
    protocol = {
        "schema_version": 1, "artifact_type": "sleepwell_endpoint_period_profiles_protocol",
        "version": "2026-10-25-endpoint-period-v1", "seed": SEED,
        "source_hashes": {cohort: _sha(path) for cohort, path in inputs.items()},
        "models": list(MODELS), "targets": list(TARGETS),
        "reference": "frozen_mean14_predictions_same_nights",
        "window": "nonoverlapping 14-day elapsed windows from each participant's first Test cutoff",
        "eligibility": "at least 4 nights and target-specific reference MAE at least 1 minute",
        "interval": "1000 moving observed-day block resamples, blocks up to 3 adjacent days, gaps over 36 hours break blocks",
        "aggregation": "participant-window median/IQR and positive fraction; equal-participant mean averages windows within person first",
        "status": "exploratory_reused_test_no_refit_no_service_promotion",
    }
    _dump(args.output / "protocol.json", protocol)

    public_rows = []
    private_counts = {}
    for cohort, path in inputs.items():
        private, summary = cohort_profiles(pd.read_parquet(path), cohort)
        private.to_csv(args.output / f"{cohort}_endpoint_period_profiles_private.csv", index=False)
        public_rows.extend(summary)
        private_counts[cohort] = len(private)
    artifact = {
        "schema_version": 1, "artifact_type": "sleepwell_endpoint_period_profiles",
        "version": protocol["version"], "test_status": "retrospective_reused_test",
        "no_service_promotion": True, "rows": public_rows,
        "limitations": [
            "Participant-window summaries weight people with more eligible windows more heavily.",
            "Equal-participant mean skill averages eligible windows within each person before the cohort mean.",
            "Short-series block intervals are exploratory and are not multiplicity-adjusted.",
            "Endpoint-specific prediction accuracy is not recommendation efficacy or longitudinal habit improvement.",
        ],
    }
    _dump(args.output / "endpoint_period_profiles_v1.json", artifact)
    _dump(args.output / "verification.json", {
        "protocol_written_before_error_calculation": True,
        "main_full_validity_artifact_overwritten": False,
        "private_rows": private_counts, "public_rows": len(public_rows),
        "cohorts": list(COHORTS), "targets": list(TARGETS), "models": list(MODELS),
        "all_public_numbers_finite_or_null": all(
            value is None or not isinstance(value, float) or math.isfinite(value)
            for row in public_rows for value in row.values()),
    })
    print(json.dumps({"public_rows": len(public_rows), "private_rows": private_counts}, indent=2))


if __name__ == "__main__":
    main()
