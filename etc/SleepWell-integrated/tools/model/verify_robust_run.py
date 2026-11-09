#!/usr/bin/env python3
"""Independently reload and verify a completed robust next-night research run."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")

import joblib
import numpy as np
import pandas as pd


COHORTS = ("cmi", "mesa", "samsung")
TARGETS = ["bed_offset", "wake_offset"]
PREDICTIONS = ["predicted_bed_offset", "predicted_wake_offset"]
TOLERANCE_MIN = 1e-7


class VerificationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    require(isinstance(value, dict), f"Expected JSON object: {path.name}")
    return value


def dump(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def wait_for_completion(run: Path, timeout_seconds: int) -> None:
    summary = run / "summary.json"
    deadline = time.monotonic() + timeout_seconds
    while not summary.is_file() and time.monotonic() < deadline:
        time.sleep(1)
    require(summary.is_file(), f"Run did not complete within {timeout_seconds} seconds")


def project(values) -> np.ndarray:
    output = []
    for bed, wake in np.asarray(values, dtype=float):
        bed = min(960.0, max(0.0, float(bed)))
        wake = min(1440.0, max(bed + 30.0, min(bed + 960.0, float(wake))))
        output.append((bed, wake))
    return np.asarray(output, dtype=float)


def row_errors(frame: pd.DataFrame) -> pd.DataFrame:
    actual = frame[TARGETS].to_numpy(float)
    predicted = frame[PREDICTIONS].to_numpy(float)
    base = np.abs(actual - predicted)
    interval = np.abs((actual[:, 1] - actual[:, 0]) - (predicted[:, 1] - predicted[:, 0]))
    return pd.DataFrame({
        "onset_mae_min": base[:, 0],
        "wake_mae_min": base[:, 1],
        "interval_mae_min": interval,
        "joint_mae_min": base.mean(axis=1),
    })


def score(frame: pd.DataFrame, cohort: str) -> dict:
    errors = row_errors(frame)
    errors["subject"] = frame["subject_id"].astype(str).to_numpy()
    values = (errors.drop(columns="subject").mean() if cohort == "samsung"
              else errors.groupby("subject").mean().mean())
    return {
        **{key: float(value) for key, value in values.items()},
        "n_nights": int(len(frame)),
        "n_people": int(frame["subject_id"].astype(str).nunique()),
        "averaging": "equal night" if cohort == "samsung" else "equal participant",
    }


def tail_summary(frame: pd.DataFrame) -> dict:
    errors = row_errors(frame)["joint_mae_min"].to_numpy(float)
    order = np.sort(errors)[::-1]
    top_count = max(1, int(np.ceil(len(order) * 0.10)))
    total = float(order.sum())
    return {
        "n": int(len(errors)),
        "joint_absolute_error_quantiles_min": {
            "p50": float(np.quantile(errors, 0.50)),
            "p90": float(np.quantile(errors, 0.90)),
            "p95": float(np.quantile(errors, 0.95)),
            "max": float(errors.max()),
        },
        "joint_error_over_120_min_n": int((errors > 120).sum()),
        "joint_error_over_240_min_n": int((errors > 240).sum()),
        "top_10pct_share_of_joint_absolute_error": float(order[:top_count].sum() / total) if total else 0.0,
    }


def same_test_rows(reference: pd.DataFrame, candidate: pd.DataFrame, label: str) -> None:
    require(len(candidate) == len(reference), f"{label}: Test row count differs")
    require(candidate["row_id"].is_unique, f"{label}: duplicate row_id")
    require(candidate["row_id"].tolist() == reference["row_id"].tolist(),
            f"{label}: row_id order differs")
    require(candidate["subject_id"].astype(str).tolist() == reference["subject_id"].astype(str).tolist(),
            f"{label}: subject alignment differs")
    left = pd.to_datetime(candidate["cutoff"], errors="raise", utc=True)
    right = pd.to_datetime(reference["cutoff"], errors="raise", utc=True)
    require(np.array_equal(left.to_numpy(), right.to_numpy()), f"{label}: cutoff alignment differs")
    require(np.allclose(candidate[TARGETS], reference[TARGETS], rtol=0, atol=TOLERANCE_MIN),
            f"{label}: target alignment differs")


def compare_metrics(actual: dict, expected: dict, label: str) -> None:
    for key in ("onset_mae_min", "wake_mae_min", "interval_mae_min", "joint_mae_min"):
        require(abs(float(actual[key]) - float(expected[key])) <= TOLERANCE_MIN,
                f"{label}: saved {key} differs")
    for key in ("n_nights", "n_people", "averaging"):
        require(actual[key] == expected[key], f"{label}: saved {key} differs")


def verify_hashes(run: Path, previous: Path, personal_sensors: Path | None) -> dict:
    protocol = load_json(run / "protocol.json")
    for relative, expected in protocol.get("input_hashes", {}).items():
        path = previous / Path(relative)
        require(path.is_file(), f"Missing frozen input: {relative}")
        require(sha(path) == expected, f"Frozen input hash differs: {relative}")
    sensor_hash = protocol.get("sensor_sha256")
    if sensor_hash is not None:
        require(personal_sensors is not None and personal_sensors.is_file(),
                "Personal sensor cache is required for hash verification")
        require(sha(personal_sensors) == sensor_hash, "Personal sensor cache hash differs")

    expected_code = load_json(run / "code_sha256.json")
    snapshot = run / "code_snapshot"
    require(set(expected_code) == {path.name for path in snapshot.iterdir() if path.is_file()},
            "Code snapshot file set differs")
    for name, expected in expected_code.items():
        require(sha(snapshot / name) == expected, f"Code snapshot hash differs: {name}")
    environment = load_json(run / "environment_sha256.json")
    require(sha(run / "environment.txt") == environment["environment.txt"],
            "Environment manifest hash differs")
    return {
        "frozen_inputs": len(protocol.get("input_hashes", {})),
        "sensor_cache": sensor_hash is not None,
        "code_snapshot_files": len(expected_code),
        "environment_manifest": True,
    }


def verify_cohort(run: Path, cohort: str, top_summary: dict) -> tuple[dict, dict, dict[str, pd.DataFrame]]:
    directory = run / cohort
    artifact = load_json(directory / "artifact.json")
    cohort_summary = load_json(directory / "summary.json")
    require(top_summary.get(cohort) == cohort_summary, f"{cohort}: top-level summary differs")
    features = pd.read_parquet(directory / "features.parquet")
    require(features["row_id"].is_unique, f"{cohort}: feature row_id is not unique")
    require(not features.drop(columns="row_id").isin([np.inf, -np.inf]).any().any(),
            f"{cohort}: infinite feature value")

    test_files = sorted(directory.glob("test_*.csv"))
    require(test_files, f"{cohort}: no Test prediction files")
    frames = {path.stem.removeprefix("test_"): pd.read_csv(path) for path in test_files}
    require("data" in frames, f"{cohort}: data Test file missing")
    reference = frames["data"]
    require(reference["row_id"].is_unique, f"{cohort}: duplicate Test row_id")
    for arm, frame in frames.items():
        same_test_rows(reference, frame, f"{cohort}/{arm}")
        require(arm in cohort_summary["test"], f"{cohort}/{arm}: summary metric missing")
        compare_metrics(score(frame, cohort), cohort_summary["test"][arm], f"{cohort}/{arm}")

    feature_index = features.set_index("row_id")
    reload_errors = {}
    model_hashes = artifact.get("model_sha256", {})
    feature_names = artifact.get("feature_names", {})
    require(set(model_hashes) == set(feature_names), f"{cohort}: model/feature arm sets differ")
    for arm, names in feature_names.items():
        model_path = directory / f"{arm}.joblib"
        require(model_path.is_file(), f"{cohort}/{arm}: model missing")
        require(sha(model_path) == model_hashes[arm], f"{cohort}/{arm}: model hash differs")
        require(arm in frames, f"{cohort}/{arm}: Test CSV missing")
        require(len(names) == len(set(names)) and set(names).issubset(features.columns),
                f"{cohort}/{arm}: invalid feature_names")
        test_features = feature_index.loc[frames[arm]["row_id"], names]
        predicted = project(joblib.load(model_path).predict(test_features.to_numpy(float)))
        saved = frames[arm][PREDICTIONS].to_numpy(float)
        maximum = float(np.max(np.abs(predicted - saved))) if len(saved) else 0.0
        require(maximum <= TOLERANCE_MIN, f"{cohort}/{arm}: reloaded predictions differ")
        reload_errors[arm] = maximum

    diagnostics = {arm: tail_summary(frame) for arm, frame in frames.items()}
    verification = {
        "test_rows": int(len(reference)),
        "test_people": int(reference["subject_id"].astype(str).nunique()),
        "arms": sorted(frames),
        "learned_arms": sorted(feature_names),
        "reload_maximum_error_min": reload_errors,
        "saved_metrics_recomputed": True,
        "row_id_alignment": True,
        "model_hashes": True,
    }
    return verification, diagnostics, frames


def group_metrics(frame: pd.DataFrame, groups: pd.Series) -> dict:
    output = {}
    for name in groups.dropna().unique():
        mask = groups.eq(name).to_numpy()
        if mask.any():
            errors = row_errors(frame.loc[mask])
            output[str(name)] = {
                "n": int(mask.sum()),
                "joint_mae_min": float(errors["joint_mae_min"].mean()),
                "onset_mae_min": float(errors["onset_mae_min"].mean()),
                "wake_mae_min": float(errors["wake_mae_min"].mean()),
            }
    return output


def samsung_gap_diagnostics(run: Path, frames: dict[str, pd.DataFrame]) -> dict:
    reference = frames["data"]
    order = np.argsort(pd.to_datetime(reference["cutoff"], utc=True).to_numpy())
    ordered = reference.iloc[order].reset_index(drop=True)
    cutoffs = pd.to_datetime(ordered["cutoff"], utc=True)
    gaps = cutoffs.diff().dt.total_seconds().div(86400)
    cutoff_group = pd.Series("first_test_row", index=ordered.index, dtype=object)
    cutoff_group.loc[gaps.notna() & gaps.le(7)] = "gap_le_7d"
    cutoff_group.loc[gaps.gt(7) & gaps.le(30)] = "gap_8_to_30d"
    cutoff_group.loc[gaps.gt(30)] = "gap_gt_30d"

    feature_index = pd.read_parquet(run / "samsung" / "features.parquet").set_index("row_id")
    history_age = pd.to_numeric(feature_index.loc[ordered["row_id"], "history_age_days"], errors="coerce").reset_index(drop=True)
    age_group = pd.Series("missing", index=ordered.index, dtype=object)
    age_group.loc[history_age.le(2)] = "history_age_le_2d"
    age_group.loc[history_age.gt(2) & history_age.le(7)] = "history_age_2_to_7d"
    age_group.loc[history_age.gt(7) & history_age.le(30)] = "history_age_7_to_30d"
    age_group.loc[history_age.gt(30)] = "history_age_gt_30d"

    by_arm = {}
    for arm, frame in frames.items():
        aligned = frame.set_index("row_id").loc[ordered["row_id"]].reset_index()
        by_arm[arm] = {
            "previous_test_cutoff_gap": group_metrics(aligned, cutoff_group),
            "history_age_at_decision": group_metrics(aligned, age_group),
        }
    return {
        "definitions": {
            "previous_test_cutoff_gap": "Difference from the preceding evaluated Samsung Test cutoff; first row separate.",
            "history_age_at_decision": "Saved causal feature: days since the latest completed prior sleep available at 20:00.",
            "purpose": "Descriptive error diagnostics only; groups do not filter rows or tune models.",
        },
        "arms": by_arm,
    }


def verify(run: Path, previous: Path, personal_sensors: Path | None) -> tuple[dict, dict]:
    require(run.is_dir() and previous.is_dir(), "Run or previous experiment directory is missing")
    top_summary = load_json(run / "summary.json")
    hashes = verify_hashes(run, previous, personal_sensors)
    cohorts, tails, samsung_frames = {}, {}, None
    for cohort in COHORTS:
        cohorts[cohort], tails[cohort], frames = verify_cohort(run, cohort, top_summary)
        if cohort == "samsung":
            samsung_frames = frames
    require(samsung_frames is not None, "Samsung Test frames missing")
    verification = {
        "schema_version": 1,
        "verified": True,
        "tolerance_minutes": TOLERANCE_MIN,
        "hashes": hashes,
        "cohorts": cohorts,
        "privacy": "Aggregate output only; participant identifiers and dates omitted.",
    }
    diagnostics = {
        "schema_version": 1,
        "all_test_error_tails": tails,
        "samsung_gap_groups": samsung_gap_diagnostics(run, samsung_frames),
        "interpretation": "Descriptive audit of the unchanged complete Test sets; no model fitting, selection, row removal, or threshold tuning.",
        "privacy": "Aggregate output only; participant identifiers and dates omitted.",
    }
    return verification, diagnostics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--previous", required=True, type=Path)
    parser.add_argument("--personal-sensors", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--wait-seconds", type=int, default=60, choices=range(0, 61))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run, previous = args.run.resolve(), args.previous.resolve()
    wait_for_completion(run, args.wait_seconds)
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError(f"Output directory is not empty: {args.output}")
    args.output.mkdir(parents=True, exist_ok=True)
    verification, diagnostics = verify(run, previous, args.personal_sensors)
    dump(args.output / "verification.json", verification)
    dump(args.output / "error_diagnostics.json", diagnostics)
    print(json.dumps({
        "verified": True,
        "output": str(args.output),
        "test_rows": {name: value["test_rows"] for name, value in verification["cohorts"].items()},
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
