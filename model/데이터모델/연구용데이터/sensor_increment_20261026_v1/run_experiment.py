"""Frozen P01 pre-20:00 sensor-increment experiment.

This executable reads only the three sources named by protocol.json and the
shared night_core constraint.  It writes a new, local analysis bundle with
exclusive-create semantics; existing results and source data are never
replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from scipy.stats import rankdata
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


EXPERIMENT_RELATIVE = Path("데이터모델/연구용데이터/sensor_increment_20261026_v1")
NIGHT_CORE_RELATIVE = Path("기타/SleepWell-integrated/web-app/night_core.py")
PARTICIPANT_ID = "P01"
SOURCE_SUBJECT_ID = "samsung_personal_001"
KST = timezone(timedelta(hours=9))
EXPECTED_SPLITS = {"train": 137, "validation": 29, "test": 30}
TARGETS = (
    "device_onset_offset_min",
    "device_wake_offset_min",
    "device_interval_min",
)
SENSOR_FEATURES = ("hr_mean", "hr_std", "evening_hr_delta", "log1p_steps")
COVERAGE_FEATURES = (
    "log1p_hr_count",
    "log1p_late_hr_count",
    "log1p_step_bins",
)
HISTORY_FEATURES = (
    "previous_onset_offset_min",
    "previous_wake_offset_min",
    "previous_interval_min",
    "last7_onset_median_min",
    "last7_wake_median_min",
    "last7_interval_median_min",
    "last7_onset_std_min",
    "last7_wake_std_min",
    "last7_interval_std_min",
    "history_observation_count",
    "history_count_last7_observations",
    "history_gap_days",
    "weekday_sin",
    "weekday_cos",
    "days_from_training_start",
)
ASSOCIATION_CONTROLS = (
    "previous_onset_offset_min",
    "previous_wake_offset_min",
    "last7_onset_median_min",
    "last7_wake_median_min",
    "last7_onset_std_min",
    "last7_wake_std_min",
    "history_gap_days",
    "weekday_sin",
    "weekday_cos",
    "days_from_training_start",
    *COVERAGE_FEATURES,
)
ARM_FEATURES: dict[str, tuple[str, ...]] = {
    "ridge_history": HISTORY_FEATURES,
    "ridge_history_coverage": HISTORY_FEATURES + COVERAGE_FEATURES,
    "sensor_hr_mean_std": HISTORY_FEATURES + COVERAGE_FEATURES + ("hr_mean", "hr_std"),
    "sensor_evening_hr_delta": HISTORY_FEATURES + COVERAGE_FEATURES + ("evening_hr_delta",),
    "sensor_log_steps": HISTORY_FEATURES + COVERAGE_FEATURES + ("log1p_steps",),
    "sensor_all_four": HISTORY_FEATURES + COVERAGE_FEATURES + SENSOR_FEATURES,
}
SENSOR_ARMS = (
    "sensor_hr_mean_std",
    "sensor_evening_hr_delta",
    "sensor_log_steps",
    "sensor_all_four",
)
PREDICTION_ARMS = ("recent7_median", *ARM_FEATURES)
REFERENCE_ARM = "ridge_history_coverage"
ASSOCIATION_TRAIN_MIN_N = 40
ASSOCIATION_VALIDATION_MIN_N = 10
SELECTION_MIN_GAIN_MIN = 5.0
RIDGE_ALPHA = 10.0
BOOTSTRAP_SEED = 20261026
BOOTSTRAP_RESAMPLES = 2000
MODEL_FILENAMES = {arm: f"{arm}.joblib" for arm in ARM_FEATURES}
OUTPUT_FILENAMES = (
    "panel.csv",
    "panel.sqlite",
    "association.csv",
    "predictions.csv",
    "metrics.json",
    "selection.json",
    "source_hashes.json",
    "verification.json",
    "audit.json",
)
EXPECTED_DENSITY_AUDIT = {
    "train": {"n_nights": 137, "calendar_span_days": 227, "median_hr_count": 909.0},
    "validation": {"n_nights": 29, "calendar_span_days": 269, "median_hr_count": 457.0},
    "test": {
        "n_nights": 30,
        "calendar_span_days": 717,
        "median_hr_count": 523.5,
        "median_inter_night_gap_days": 6.0,
        "max_inter_night_gap_days": 130.0,
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if value is pd.NaT or value is None:
        return None
    return value


def write_json_exclusive(path: Path, payload: Any) -> None:
    text = json.dumps(_json_ready(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.write("\n")


def write_csv_exclusive(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, mode="x", encoding="utf-8", lineterminator="\n", na_rep="")


def planned_output_paths(output_dir: Path) -> list[Path]:
    paths = [output_dir / name for name in OUTPUT_FILENAMES]
    paths.append(output_dir / "models")
    paths.extend(output_dir / "models" / name for name in MODEL_FILENAMES.values())
    return paths


def guard_outputs(output_dir: Path) -> None:
    if not output_dir.is_dir():
        raise FileNotFoundError(f"Experiment directory does not exist: {output_dir}")
    existing = [str(path) for path in planned_output_paths(output_dir) if path.exists()]
    if existing:
        raise FileExistsError("Refusing to overwrite planned outputs: " + ", ".join(existing))


def _resolve_workspace_file(workspace: Path, relative: str | Path) -> Path:
    root = workspace.resolve()
    candidate = (root / Path(relative)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Source escapes workspace: {relative}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def load_protocol(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        protocol = json.load(handle)
    if tuple(protocol.get("targets", ())) != TARGETS:
        raise ValueError("Protocol targets do not match the frozen three-target family")
    hypotheses = tuple(item.get("feature") for item in protocol.get("hypotheses", ()))
    if hypotheses != SENSOR_FEATURES:
        raise ValueError("Protocol hypotheses do not match the frozen four sensor features")
    if protocol.get("association_analysis", {}).get("family_size") != 12:
        raise ValueError("Protocol association family must remain fixed at 12")
    if set(protocol.get("sources", {})) != {"episodes", "sensor_cache", "sensor_metadata"}:
        raise ValueError("Protocol must name exactly the frozen three input sources")
    return protocol


def _parse_kst_series(series: pd.Series, label: str) -> pd.Series:
    parsed: list[pd.Timestamp] = []
    for raw in series:
        try:
            value = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid {label} timestamp: {raw!r}") from exc
        if value.tzinfo is None or value.utcoffset() != timedelta(hours=9):
            raise ValueError(f"{label} must carry an explicit +09:00 offset: {raw!r}")
        parsed.append(pd.Timestamp(value.astimezone(KST)))
    return pd.Series(parsed, index=series.index, name=series.name)


def _stable_target_id(cutoff: pd.Timestamp) -> str:
    token = hashlib.sha256(f"{PARTICIPANT_ID}|{cutoff.isoformat()}".encode("utf-8")).hexdigest()[:20]
    return f"{PARTICIPANT_ID}_{token}"


def prepare_p01_episodes(episodes: pd.DataFrame) -> pd.DataFrame:
    required = {
        "cohort",
        "subject_id",
        "split",
        "cutoff",
        "bed",
        "wake",
        "observed_at",
        "bedtime_min",
        "wake_min",
        "duration_min",
    }
    missing = required.difference(episodes.columns)
    if missing:
        raise ValueError(f"Episode source is missing columns: {sorted(missing)}")
    source = episodes.loc[
        (episodes["cohort"] == "Samsung") & (episodes["subject_id"] == SOURCE_SUBJECT_ID)
    ].copy()
    if len(source) != 196:
        raise ValueError(f"Frozen P01 panel must contain 196 rows, found {len(source)}")
    counts = source["split"].value_counts().to_dict()
    if counts != EXPECTED_SPLITS:
        raise ValueError(f"Frozen P01 split changed: {counts}")
    for column in ("cutoff", "bed", "wake", "observed_at"):
        source[column] = _parse_kst_series(source[column], column)
    source = source.sort_values("cutoff", kind="stable").reset_index(drop=True)
    if source["cutoff"].duplicated().any():
        raise ValueError("P01 episode cutoffs must be unique")
    if not source["cutoff"].map(
        lambda value: (value.hour, value.minute, value.second, value.microsecond) == (20, 0, 0, 0)
    ).all():
        raise ValueError("Every forecast cutoff must be exactly 20:00 +09:00")
    split_rank = source["split"].map({"train": 0, "validation": 1, "test": 2})
    if split_rank.isna().any() or not split_rank.is_monotonic_increasing:
        raise ValueError("Frozen split order must be chronological train/validation/test")

    onset = (source["bed"] - source["cutoff"]).dt.total_seconds() / 60.0
    wake = (source["wake"] - source["cutoff"]).dt.total_seconds() / 60.0
    interval = (source["wake"] - source["bed"]).dt.total_seconds() / 60.0
    supplied = source[["bedtime_min", "wake_min", "duration_min"]].apply(pd.to_numeric, errors="raise")
    derived = np.column_stack((onset, wake, interval))
    if not np.allclose(supplied.to_numpy(dtype=float), derived, atol=1e-8, rtol=0.0):
        raise ValueError("Episode timestamps and supplied timing targets disagree")
    if not ((onset >= 0) & (onset <= 960)).all():
        raise ValueError("Observed onset lies outside the declared constraint support")
    if not ((interval >= 30) & (interval <= 960) & (wake <= 1440)).all():
        raise ValueError("Observed wake/interval lies outside the declared constraint support")

    panel = pd.DataFrame(
        {
            "participant_id": PARTICIPANT_ID,
            "target_id": [_stable_target_id(value) for value in source["cutoff"]],
            "split": source["split"].to_numpy(),
            "cutoff": source["cutoff"],
            "sleep_start": source["bed"],
            "sleep_end": source["wake"],
            "observed_at": source["observed_at"],
            TARGETS[0]: onset.to_numpy(dtype=float),
            TARGETS[1]: wake.to_numpy(dtype=float),
            TARGETS[2]: interval.to_numpy(dtype=float),
        }
    )
    if panel["target_id"].duplicated().any():
        raise ValueError("Stable target IDs must be unique")
    return panel


def _population_std(values: pd.Series) -> float:
    array = values.to_numpy(dtype=float)
    return float(np.std(array, ddof=0)) if len(array) else float("nan")


def derive_history_features(targets: pd.DataFrame) -> pd.DataFrame:
    """Derive history using only records whose cutoff, wake, and receipt precede target cutoff."""
    required = {"cutoff", "sleep_end", "observed_at", *TARGETS}
    missing = required.difference(targets.columns)
    if missing:
        raise ValueError(f"History source is missing columns: {sorted(missing)}")
    if targets["cutoff"].duplicated().any():
        raise ValueError("Target cutoffs must be unique before history derivation")
    rows: list[dict[str, Any]] = []
    for target in targets.itertuples():
        cutoff = target.cutoff
        eligible = targets.loc[
            (targets["cutoff"] < cutoff)
            & (targets["sleep_end"] < cutoff)
            & (targets["observed_at"] < cutoff)
        ].sort_values(["sleep_end", "observed_at", "cutoff"], kind="stable")
        recent = eligible.tail(7)
        previous = eligible.tail(1)
        record: dict[str, Any] = {
            "history_observation_count": float(len(eligible)),
            "history_count_last7_observations": float(len(recent)),
            "history_gap_days": (
                float((cutoff - eligible.iloc[-1]["sleep_end"]).total_seconds() / 86400.0)
                if len(eligible)
                else float("nan")
            ),
            "max_history_source_cutoff": eligible["cutoff"].max() if len(eligible) else pd.NaT,
            "max_history_wake_at": eligible["sleep_end"].max() if len(eligible) else pd.NaT,
            "max_history_observed_at": eligible["observed_at"].max() if len(eligible) else pd.NaT,
        }
        for target_name, short_name in (
            (TARGETS[0], "onset"),
            (TARGETS[1], "wake"),
            (TARGETS[2], "interval"),
        ):
            record[f"previous_{short_name}_offset_min" if short_name != "interval" else "previous_interval_min"] = (
                float(previous.iloc[0][target_name]) if len(previous) else float("nan")
            )
            record[f"last7_{short_name}_median_min"] = (
                float(recent[target_name].median()) if len(recent) else float("nan")
            )
            record[f"last7_{short_name}_std_min"] = _population_std(recent[target_name])
        rows.append(record)
    history = pd.DataFrame(rows, index=targets.index)
    expected = set(HISTORY_FEATURES).difference({"weekday_sin", "weekday_cos", "days_from_training_start"})
    if not expected.issubset(history.columns):
        raise AssertionError("Internal history feature construction is incomplete")
    return history


def derive_calendar_features(targets: pd.DataFrame) -> pd.DataFrame:
    train = targets.loc[targets["split"] == "train", "cutoff"]
    if train.empty:
        raise ValueError("Training start is unavailable")
    start_date = train.min().date()
    weekdays = targets["cutoff"].map(lambda value: value.weekday()).to_numpy(dtype=float)
    angle = 2.0 * math.pi * weekdays / 7.0
    return pd.DataFrame(
        {
            "weekday_sin": np.sin(angle),
            "weekday_cos": np.cos(angle),
            "days_from_training_start": targets["cutoff"].map(
                lambda value: float((value.date() - start_date).days)
            ),
        },
        index=targets.index,
    )


def derive_sensor_features(cache: pd.DataFrame) -> pd.DataFrame:
    required = {"hr_mean", "hr_std", "hr_count", "late_hr_mean", "late_hr_count", "steps", "step_bins"}
    missing = required.difference(cache.columns)
    if missing:
        raise ValueError(f"Sensor cache is missing columns: {sorted(missing)}")
    numeric = cache[list(required)].apply(pd.to_numeric, errors="coerce")
    for column in ("hr_count", "late_hr_count", "step_bins"):
        values = numeric[column]
        if values.isna().any() or (values < 0).any() or not np.allclose(values, np.round(values)):
            raise ValueError(f"{column} must contain finite nonnegative integer counts")
    if (numeric["steps"].dropna() < 0).any():
        raise ValueError("Observed step sums cannot be negative")

    hr_bins = numeric["hr_count"] > 0
    late_hr_bins = numeric["late_hr_count"] > 0
    step_export_bins = numeric["step_bins"] > 0
    hr_mean = numeric["hr_mean"].where(hr_bins)
    hr_std = numeric["hr_std"].where(numeric["hr_count"] >= 2)
    late_hr_mean = numeric["late_hr_mean"].where(late_hr_bins)
    step_values = numeric["steps"].where(step_export_bins)
    result = pd.DataFrame(index=cache.index)
    result["hr_count"] = numeric["hr_count"].astype(int)
    result["late_hr_count"] = numeric["late_hr_count"].astype(int)
    result["step_bins"] = numeric["step_bins"].astype(int)
    result["pre20_steps"] = step_values
    result["hr_mean"] = hr_mean
    result["hr_std"] = hr_std
    result["evening_hr_delta"] = (late_hr_mean - hr_mean).where(late_hr_bins & hr_bins)
    result["log1p_steps"] = np.log1p(step_values)
    result["log1p_hr_count"] = np.log1p(numeric["hr_count"])
    result["log1p_late_hr_count"] = np.log1p(numeric["late_hr_count"])
    result["log1p_step_bins"] = np.log1p(numeric["step_bins"])
    result["hr_export_bins_present"] = hr_bins
    result["late_hr_export_bins_present"] = late_hr_bins
    result["step_export_bins_present"] = step_export_bins
    return result


def join_sensor_cache(panel: pd.DataFrame, cache: pd.DataFrame) -> pd.DataFrame:
    if "cutoff" not in panel or "cutoff" not in cache:
        raise ValueError("Both panel and sensor cache require cutoff keys")
    left = panel.copy()
    right = cache.copy()
    left["_join_cutoff"] = pd.to_datetime(left["cutoff"], errors="raise", utc=True)
    right["_join_cutoff"] = pd.to_datetime(right["cutoff"], errors="raise", utc=True)
    if left["_join_cutoff"].duplicated().any():
        raise ValueError("Panel contains duplicate cutoff keys")
    if right["_join_cutoff"].duplicated().any():
        raise ValueError("Sensor cache contains duplicate cutoff keys")
    left_keys = set(left["_join_cutoff"])
    right_keys = set(right["_join_cutoff"])
    if left_keys != right_keys:
        missing = len(left_keys - right_keys)
        unexpected = len(right_keys - left_keys)
        raise ValueError(f"Sensor cutoff mismatch: missing={missing}, unexpected={unexpected}")
    right = right.drop(columns=["cutoff"])
    overlap = set(left.columns).intersection(right.columns).difference({"_join_cutoff"})
    if overlap:
        raise ValueError(f"Sensor join would overwrite panel columns: {sorted(overlap)}")
    joined = left.merge(right, on="_join_cutoff", how="left", validate="one_to_one", sort=False)
    return joined.drop(columns=["_join_cutoff"]).sort_values("cutoff", kind="stable").reset_index(drop=True)


def verify_history_availability(panel: pd.DataFrame) -> None:
    for column in ("max_history_source_cutoff", "max_history_wake_at", "max_history_observed_at"):
        present = panel[column].notna()
        if not (panel.loc[present, column] < panel.loc[present, "cutoff"]).all():
            raise ValueError(f"Non-past history entered features through {column}")


def make_ridge_pipeline() -> Pipeline:
    return Pipeline(
        steps=[
            (
                "imputer",
                SimpleImputer(strategy="median", keep_empty_features=True, add_indicator=True),
            ),
            ("scaler", StandardScaler()),
            ("ridge", Ridge(alpha=RIDGE_ALPHA)),
        ]
    )


def fit_models(panel: pd.DataFrame) -> dict[str, Pipeline]:
    train = panel.loc[panel["split"] == "train"].copy()
    if len(train) != EXPECTED_SPLITS["train"]:
        raise ValueError("Models may only fit the frozen 137 TRAIN nights")
    y = train[[TARGETS[0], TARGETS[1]]].to_numpy(dtype=float)
    models: dict[str, Pipeline] = {}
    for arm, features in ARM_FEATURES.items():
        model = make_ridge_pipeline()
        model.fit(train[list(features)], y)
        models[arm] = model
    return models


def persist_models_exclusive(models: dict[str, Pipeline], output_dir: Path) -> tuple[dict[str, Path], dict[str, str]]:
    model_dir = output_dir / "models"
    model_dir.mkdir(exist_ok=False)
    paths: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for arm in ARM_FEATURES:
        path = model_dir / MODEL_FILENAMES[arm]
        with path.open("xb") as handle:
            joblib.dump(models[arm], handle, compress=3)
        paths[arm] = path
        hashes[arm] = sha256_file(path)
    return paths, hashes


def load_night_core_constrain(path: Path) -> Callable[[float, float], tuple[float, float]]:
    spec = importlib.util.spec_from_file_location("sleepwell_sensor_increment_night_core", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load night_core from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    constrain = getattr(module, "constrain", None)
    if not callable(constrain):
        raise AttributeError("night_core.constrain is unavailable")
    return constrain


def training_prior(panel: pd.DataFrame) -> tuple[float, float]:
    train = panel.loc[panel["split"] == "train"]
    return (
        float(train[TARGETS[0]].median()),
        float(train[TARGETS[1]].median()),
    )


def predict_split(
    panel: pd.DataFrame,
    split: str,
    models: dict[str, Pipeline],
    model_hashes: dict[str, str],
    constrain: Callable[[float, float], tuple[float, float]],
    prior: tuple[float, float],
    status: str,
) -> pd.DataFrame:
    subset = panel.loc[panel["split"] == split].sort_values("cutoff", kind="stable").copy()
    if subset.empty:
        raise ValueError(f"No rows for split {split}")
    raw_predictions: dict[str, np.ndarray] = {}
    recent_onset = subset["last7_onset_median_min"].fillna(prior[0]).to_numpy(dtype=float)
    recent_wake = subset["last7_wake_median_min"].fillna(prior[1]).to_numpy(dtype=float)
    raw_predictions["recent7_median"] = np.column_stack((recent_onset, recent_wake))
    for arm, features in ARM_FEATURES.items():
        prediction = np.asarray(models[arm].predict(subset[list(features)]), dtype=float)
        if prediction.shape != (len(subset), 2):
            raise ValueError(f"Unexpected prediction shape for {arm}: {prediction.shape}")
        raw_predictions[arm] = prediction

    output: list[dict[str, Any]] = []
    for arm in PREDICTION_ARMS:
        for position, (_, night) in enumerate(subset.iterrows()):
            raw_onset, raw_wake = raw_predictions[arm][position]
            predicted_onset, predicted_wake = constrain(float(raw_onset), float(raw_wake))
            actual_onset = float(night[TARGETS[0]])
            actual_wake = float(night[TARGETS[1]])
            predicted_interval = predicted_wake - predicted_onset
            actual_interval = float(night[TARGETS[2]])
            onset_error = abs(predicted_onset - actual_onset)
            wake_error = abs(predicted_wake - actual_wake)
            interval_error = abs(predicted_interval - actual_interval)
            output.append(
                {
                    "participant_id": PARTICIPANT_ID,
                    "target_id": night["target_id"],
                    "split": split,
                    "cutoff": night["cutoff"],
                    "arm": arm,
                    "raw_predicted_onset_offset_min": float(raw_onset),
                    "raw_predicted_wake_offset_min": float(raw_wake),
                    "predicted_onset_offset_min": predicted_onset,
                    "predicted_wake_offset_min": predicted_wake,
                    "predicted_interval_min": predicted_interval,
                    "actual_onset_offset_min": actual_onset,
                    "actual_wake_offset_min": actual_wake,
                    "actual_interval_min": actual_interval,
                    "onset_abs_error_min": onset_error,
                    "wake_abs_error_min": wake_error,
                    "interval_abs_error_min": interval_error,
                    "joint_abs_error_min": (onset_error + wake_error) / 2.0,
                    "interval_signed_error_min": predicted_interval - actual_interval,
                    "constraint_changed": not np.allclose(
                        [raw_onset, raw_wake], [predicted_onset, predicted_wake], atol=1e-12, rtol=0.0
                    ),
                    "model_sha256": model_hashes.get(arm),
                    "fit_split": "none_past_only" if arm == "recent7_median" else "train",
                    "evaluation_status": status,
                }
            )
    predictions = pd.DataFrame(output)
    expected = len(subset) * len(PREDICTION_ARMS)
    if len(predictions) != expected:
        raise AssertionError("Prediction output is incomplete")
    return predictions


def summarize_arm(group: pd.DataFrame) -> dict[str, Any]:
    return {
        "n_nights": int(len(group)),
        "onset_mae_min": float(group["onset_abs_error_min"].mean()),
        "wake_mae_min": float(group["wake_abs_error_min"].mean()),
        "interval_mae_min": float(group["interval_abs_error_min"].mean()),
        "joint_onset_wake_mae_min": float(group["joint_abs_error_min"].mean()),
        "onset_median_absolute_error_min": float(group["onset_abs_error_min"].median()),
        "wake_median_absolute_error_min": float(group["wake_abs_error_min"].median()),
        "interval_median_absolute_error_min": float(group["interval_abs_error_min"].median()),
        "joint_onset_wake_median_absolute_error_min": float(group["joint_abs_error_min"].median()),
        "mean_signed_predicted_interval_minus_actual_min": float(group["interval_signed_error_min"].mean()),
    }


def metrics_by_split(predictions: pd.DataFrame) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for split in ("validation", "test"):
        result[split] = {}
        for arm in PREDICTION_ARMS:
            group = predictions.loc[(predictions["split"] == split) & (predictions["arm"] == arm)]
            if not group.empty:
                result[split][arm] = summarize_arm(group)
    return result


def select_sensor_arm(predictions: pd.DataFrame, threshold: float = SELECTION_MIN_GAIN_MIN) -> dict[str, Any]:
    """Select from validation only; TEST rows, if supplied, are deliberately ignored."""
    validation = predictions.loc[predictions["split"] == "validation"].copy()
    if validation.empty:
        raise ValueError("Validation predictions are required for selection")
    expected_arms = {REFERENCE_ARM, "recent7_median", *SENSOR_ARMS}
    if not expected_arms.issubset(set(validation["arm"])):
        raise ValueError("Validation predictions do not contain every frozen selection arm")
    keys = set(validation.loc[validation["arm"] == REFERENCE_ARM, "target_id"])
    if not keys:
        raise ValueError("Reference validation nights are unavailable")
    arm_mae: dict[str, float] = {}
    for arm in (REFERENCE_ARM, *SENSOR_ARMS):
        group = validation.loc[validation["arm"] == arm]
        if group["target_id"].duplicated().any() or set(group["target_id"]) != keys:
            raise ValueError(f"Selection arm {arm} is not paired on identical validation nights")
        arm_mae[arm] = float(group["joint_abs_error_min"].mean())
    best_sensor = min(SENSOR_ARMS, key=lambda arm: (arm_mae[arm], SENSOR_ARMS.index(arm)))
    improvement = arm_mae[REFERENCE_ARM] - arm_mae[best_sensor]
    selected = best_sensor if improvement >= threshold else REFERENCE_ARM
    recent = validation.loc[validation["arm"] == "recent7_median"]
    if recent["target_id"].duplicated().any() or set(recent["target_id"]) != keys:
        raise ValueError("Recent-seven benchmark is not paired on identical validation nights")
    return {
        "selected_arm": selected,
        "selected_is_sensor_arm": selected in SENSOR_ARMS,
        "best_sensor_arm": best_sensor,
        "best_sensor_gain_vs_history_coverage_min": float(improvement),
        "minimum_required_gain_min": float(threshold),
        "validation_joint_onset_wake_mae_min": arm_mae,
        "recent7_validation_joint_onset_wake_mae_benchmark_min": float(
            recent["joint_abs_error_min"].mean()
        ),
        "recent7_used_for_selection": False,
        "validation_n_nights": len(keys),
        "selection_split": "validation",
        "fit_split": "train",
        "test_targets_consulted": False,
        "tie_break_order": list(SENSOR_ARMS),
    }


def _correlation(left: np.ndarray, right: np.ndarray) -> float:
    x = np.asarray(left, dtype=float)
    y = np.asarray(right, dtype=float)
    if len(x) != len(y) or len(x) < 2 or not (np.isfinite(x).all() and np.isfinite(y).all()):
        return float("nan")
    x = x - x.mean()
    y = y - y.mean()
    denominator = math.sqrt(float(np.dot(x, x) * np.dot(y, y)))
    return float(np.dot(x, y) / denominator) if denominator > 0 else float("nan")


def cyclic_shift_p(left_residual: np.ndarray, right_residual: np.ndarray) -> tuple[float, int]:
    observed = _correlation(left_residual, right_residual)
    n = len(left_residual)
    if not math.isfinite(observed) or n < 2:
        return 1.0, 0
    null = np.asarray(
        [_correlation(left_residual, np.roll(right_residual, shift)) for shift in range(1, n)],
        dtype=float,
    )
    if len(null) != n - 1 or not np.isfinite(null).all():
        return 1.0, 0
    exceedances = int(np.count_nonzero(np.abs(null) >= abs(observed) - 1e-15))
    return float((1 + exceedances) / (1 + len(null))), int(len(null))


def association_stat(
    frame: pd.DataFrame,
    sensor: str,
    target: str,
    controls: Sequence[str],
    minimum_rows: int,
    compute_null: bool = True,
) -> dict[str, Any]:
    ordered = frame.sort_values("cutoff", kind="stable") if "cutoff" in frame else frame.copy()
    raw = ordered[[sensor, target]].apply(pd.to_numeric, errors="coerce").dropna()
    raw_rho = (
        _correlation(rankdata(raw[sensor]), rankdata(raw[target])) if len(raw) >= 2 else float("nan")
    )
    columns = [sensor, target, *controls]
    complete = ordered[columns].apply(pd.to_numeric, errors="coerce").dropna()
    result: dict[str, Any] = {
        "raw_n": int(len(raw)),
        "raw_spearman_rho": raw_rho,
        "complete_n": int(len(complete)),
        "partial_rank_residual_rho": float("nan"),
        "cyclic_shift_p_value": 1.0 if compute_null else None,
        "nonzero_rotation_count": 0,
        "status": "insufficient_complete_rows",
        "minimum_rows": int(minimum_rows),
    }
    if len(complete) < minimum_rows:
        return result
    ranked_sensor = rankdata(complete[sensor].to_numpy(dtype=float))
    ranked_target = rankdata(complete[target].to_numpy(dtype=float))
    if not (math.isfinite(_correlation(ranked_sensor, ranked_target))):
        result["status"] = "zero_variance_sensor_or_target"
        return result
    ranked_controls = np.column_stack(
        [rankdata(complete[column].to_numpy(dtype=float)) for column in controls]
    )
    design = np.column_stack((np.ones(len(complete)), ranked_controls))
    sensor_residual = ranked_sensor - design @ np.linalg.lstsq(design, ranked_sensor, rcond=None)[0]
    target_residual = ranked_target - design @ np.linalg.lstsq(design, ranked_target, rcond=None)[0]
    partial = _correlation(sensor_residual, target_residual)
    result["control_design_rank"] = int(np.linalg.matrix_rank(design))
    if not math.isfinite(partial):
        result["status"] = "zero_variance_rank_residual"
        return result
    result["partial_rank_residual_rho"] = partial
    if compute_null:
        p_value, rotations = cyclic_shift_p(sensor_residual, target_residual)
        if rotations != len(complete) - 1:
            result["status"] = "unestimable_cyclic_null"
            return result
        result["cyclic_shift_p_value"] = p_value
        result["nonzero_rotation_count"] = rotations
        result["status"] = "estimable_exploratory"
    else:
        result["cyclic_shift_p_value"] = None
        result["status"] = "estimable_descriptive_direction_only"
    return result


def benjamini_hochberg_fixed(p_values: Sequence[float], family_size: int = 12) -> list[float]:
    if len(p_values) != family_size:
        raise ValueError(f"BH family must contain exactly {family_size} p-values")
    values = np.asarray(
        [float(value) if value is not None and math.isfinite(float(value)) else 1.0 for value in p_values],
        dtype=float,
    )
    values = np.clip(values, 0.0, 1.0)
    order = np.argsort(values, kind="stable")
    ordered = values[order]
    adjusted = np.empty(family_size, dtype=float)
    running = 1.0
    for index in range(family_size - 1, -1, -1):
        running = min(running, ordered[index] * family_size / (index + 1))
        adjusted[index] = min(1.0, running)
    restored = np.empty(family_size, dtype=float)
    restored[order] = adjusted
    return restored.tolist()


def compute_associations(panel: pd.DataFrame, protocol: dict[str, Any]) -> pd.DataFrame:
    meanings = {item["feature"]: item["meaning"] for item in protocol["hypotheses"]}
    train = panel.loc[panel["split"] == "train"]
    validation = panel.loc[panel["split"] == "validation"]
    records: list[dict[str, Any]] = []
    for sensor in SENSOR_FEATURES:
        for target in TARGETS:
            train_result = association_stat(
                train, sensor, target, ASSOCIATION_CONTROLS, ASSOCIATION_TRAIN_MIN_N, compute_null=True
            )
            validation_result = association_stat(
                validation,
                sensor,
                target,
                ASSOCIATION_CONTROLS,
                ASSOCIATION_VALIDATION_MIN_N,
                compute_null=False,
            )
            train_rho = train_result["partial_rank_residual_rho"]
            validation_rho = validation_result["partial_rank_residual_rho"]
            direction_agrees: bool | None = None
            if math.isfinite(float(train_rho)) and math.isfinite(float(validation_rho)):
                direction_agrees = bool(np.sign(train_rho) == np.sign(validation_rho))
            records.append(
                {
                    "hypothesis_id": f"{sensor}__{target}",
                    "sensor_feature": sensor,
                    "sensor_meaning": meanings[sensor],
                    "target": target,
                    "train_raw_n": train_result["raw_n"],
                    "train_raw_spearman_rho": train_result["raw_spearman_rho"],
                    "train_complete_n": train_result["complete_n"],
                    "train_partial_rank_residual_rho": train_rho,
                    "train_cyclic_shift_p_value": train_result["cyclic_shift_p_value"],
                    "train_nonzero_rotation_count": train_result["nonzero_rotation_count"],
                    "train_status": train_result["status"],
                    "validation_raw_n": validation_result["raw_n"],
                    "validation_raw_spearman_rho": validation_result["raw_spearman_rho"],
                    "validation_complete_n": validation_result["complete_n"],
                    "validation_partial_rank_residual_rho": validation_rho,
                    "validation_status": validation_result["status"],
                    "validation_direction_agrees": direction_agrees,
                    "validation_inference": "descriptive_direction_only_no_p_value",
                    "causal_interpretation": "not_supported",
                }
            )
    if len(records) != 12:
        raise AssertionError("The frozen association family must contain 12 rows")
    p_values = [
        float(record["train_cyclic_shift_p_value"])
        if record["train_status"] == "estimable_exploratory"
        else 1.0
        for record in records
    ]
    adjusted = benjamini_hochberg_fixed(p_values, family_size=12)
    for record, p_value, q_value in zip(records, p_values, adjusted):
        record["train_cyclic_shift_p_value"] = p_value
        record["bh_q_value_fixed_family_12"] = q_value
        record["bh_family_size"] = 12
        record["unestimable_included_as_p1"] = record["train_status"] != "estimable_exploratory"
    return pd.DataFrame(records)


def paired_night_gains(
    predictions: pd.DataFrame,
    candidate_arm: str,
    reference_arm: str,
    split: str = "test",
    error_column: str = "joint_abs_error_min",
) -> pd.DataFrame:
    key_columns = ["participant_id", "target_id", "cutoff"]
    candidate = predictions.loc[
        (predictions["split"] == split) & (predictions["arm"] == candidate_arm),
        [*key_columns, error_column],
    ].copy()
    reference = predictions.loc[
        (predictions["split"] == split) & (predictions["arm"] == reference_arm),
        [*key_columns, error_column],
    ].copy()
    if candidate.duplicated(key_columns).any() or reference.duplicated(key_columns).any():
        raise ValueError("Paired metrics require one row per participant/night/arm")
    candidate_keys = set(map(tuple, candidate[key_columns].to_numpy()))
    reference_keys = set(map(tuple, reference[key_columns].to_numpy()))
    if not candidate_keys or candidate_keys != reference_keys:
        raise ValueError("Paired metrics require identical same-night keys")
    paired = candidate.merge(
        reference,
        on=key_columns,
        how="inner",
        validate="one_to_one",
        suffixes=("_candidate", "_reference"),
    )
    paired["gain_min"] = paired[f"{error_column}_reference"] - paired[f"{error_column}_candidate"]
    return paired


def cluster_bootstrap_gain(
    paired: pd.DataFrame,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    local_cutoff = pd.to_datetime(paired["cutoff"], errors="raise", utc=True).dt.tz_convert(KST)
    calendar = local_cutoff.dt.isocalendar()
    working = paired.copy()
    working["calendar_week"] = calendar["year"].astype(str) + "-W" + calendar["week"].astype(str).str.zfill(2)
    groups = [group["gain_min"].to_numpy(dtype=float) for _, group in working.groupby("calendar_week", sort=True)]
    if not groups:
        raise ValueError("No calendar-week clusters for bootstrap")
    rng = np.random.default_rng(seed)
    estimates = np.empty(resamples, dtype=float)
    for index in range(resamples):
        selected = rng.integers(0, len(groups), size=len(groups))
        estimates[index] = float(np.concatenate([groups[item] for item in selected]).mean())
    return {
        "n_paired_nights": int(len(working)),
        "n_calendar_week_clusters": int(len(groups)),
        "observed_mean_gain_min": float(working["gain_min"].mean()),
        "bootstrap_median_gain_min": float(np.median(estimates)),
        "bootstrap_percentile_95_interval_min": [
            float(np.quantile(estimates, 0.025)),
            float(np.quantile(estimates, 0.975)),
        ],
        "resamples": int(resamples),
        "seed": int(seed),
        "gain_definition": "reference joint error minus candidate joint error; positive favors candidate",
        "inference": "exploratory descriptive uncertainty, not population significance",
    }


def test_paired_gains(predictions: pd.DataFrame, selected_arm: str) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    candidates = [arm for arm in PREDICTION_ARMS if arm not in {REFERENCE_ARM, "recent7_median"}]
    if selected_arm not in candidates and selected_arm != REFERENCE_ARM:
        candidates.append(selected_arm)
    for candidate in candidates:
        for reference in (REFERENCE_ARM, "recent7_median"):
            paired = paired_night_gains(predictions, candidate, reference, split="test")
            result = cluster_bootstrap_gain(paired)
            result.update(
                {
                    "candidate_arm": candidate,
                    "reference_arm": reference,
                    "candidate_was_validation_selected": candidate == selected_arm,
                }
            )
            comparisons.append(result)
    if selected_arm == REFERENCE_ARM:
        paired = paired_night_gains(predictions, REFERENCE_ARM, "recent7_median", split="test")
        result = cluster_bootstrap_gain(paired)
        result.update(
            {
                "candidate_arm": REFERENCE_ARM,
                "reference_arm": "recent7_median",
                "candidate_was_validation_selected": True,
            }
        )
        comparisons.append(result)
    return comparisons


def split_density_summary(panel: pd.DataFrame) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for split in ("train", "validation", "test"):
        group = panel.loc[panel["split"] == split].sort_values("cutoff", kind="stable")
        dates = group["cutoff"].map(lambda value: value.date())
        gaps = panel.sort_values("cutoff", kind="stable")["cutoff"].diff().dt.total_seconds().div(86400).loc[group.index].dropna().to_numpy()
        span = (dates.max() - dates.min()).days + 1
        summary[split] = {
            "n_nights": int(len(group)),
            "cutoff_start": group["cutoff"].min(),
            "cutoff_end": group["cutoff"].max(),
            "calendar_span_days": int(span),
            "observed_night_density": float(len(group) / span),
            "median_inter_night_gap_days": float(np.median(gaps)) if len(gaps) else None,
            "max_inter_night_gap_days": float(np.max(gaps)) if len(gaps) else None,
            "median_hr_count": float(group["hr_count"].median()),
            "hr_mean_nonmissing_n": int(group["hr_mean"].notna().sum()),
            "median_late_hr_count": float(group["late_hr_count"].median()),
            "evening_hr_delta_nonmissing_n": int(group["evening_hr_delta"].notna().sum()),
            "median_step_bins": float(group["step_bins"].median()),
            "step_export_bins_present_n": int(group["step_export_bins_present"].sum()),
            "zero_step_sum_with_export_bins_n": int(
                ((group["pre20_steps"] == 0) & group["step_export_bins_present"]).sum()
            ),
            "coverage_interpretation": (
                "Counts describe populated completed export bins, not verified wearing, active sensing, "
                "measurement quality, or physiology."
            ),
        }
    return summary


def verify_density_audit(summary: dict[str, dict[str, Any]]) -> None:
    for split, expected in EXPECTED_DENSITY_AUDIT.items():
        actual = summary[split]
        for key, value in expected.items():
            if not math.isclose(float(actual[key]), float(value), rel_tol=0.0, abs_tol=1e-9):
                raise ValueError(f"Frozen density audit changed for {split}.{key}: {actual[key]} != {value}")


def collection_priorities(
    density: dict[str, dict[str, Any]], selection: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        {
            "rank": 1,
            "collection": "independent morning sleep-timing reference",
            "fields": [
                "sleep_onset_at",
                "final_wake_at",
                "measurement_method",
                "device_clock_offset",
                "received_at",
                "revised_at",
            ],
            "rationale": (
                "Current outcomes are consumer-device episode boundaries, not an independent timing or PSG reference."
            ),
            "responses_collected_in_this_run": 0,
            "ranking_dimensions": ["independent_timing", "temporal_overlap"],
        },
        {
            "rank": 2,
            "collection": "immutable sensor availability and revision provenance",
            "fields": ["bin_start_at", "bin_end_at", "received_at", "revised_at", "source_timezone"],
            "rationale": (
                "Historical receipt/revision times are unknown. Completed-bin counts support only a retrospective "
                "availability assumption; the TEST span is sparse and drifted."
            ),
            "evidence": {
                "train_median_hr_count": density["train"]["median_hr_count"],
                "validation_median_hr_count": density["validation"]["median_hr_count"],
                "test_median_hr_count": density["test"]["median_hr_count"],
                "test_median_gap_days": density["test"]["median_inter_night_gap_days"],
                "test_max_gap_days": density["test"]["max_inter_night_gap_days"],
            },
            "responses_collected_in_this_run": 0,
            "ranking_dimensions": ["coverage", "temporal_overlap"],
        },
        {
            "rank": 3,
            "collection": "prospective pre-cutoff evening context",
            "fields": [
                "intended_bedtime_at",
                "next_morning_constraint_at",
                "last_caffeine_at",
                "evening_fatigue_scale",
                "received_at",
                "revised_at",
            ],
            "rationale": (
                "No timely linked survey nights exist. These fields must be received before 20:00 and evaluated "
                "as a prespecified future increment rather than backfilled."
            ),
            "validation_sensor_gain_vs_history_coverage_min": selection[
                "best_sensor_gain_vs_history_coverage_min"
            ],
            "responses_collected_in_this_run": 0,
            "ranking_dimensions": ["temporal_overlap", "validation_incremental_value"],
        },
        {
            "rank": 4,
            "collection": "prospective daytime function and morning outcome",
            "fields": [
                "morning_restoration_scale",
                "daytime_sleepiness_scale",
                "daytime_function_scale",
                "received_at",
                "revised_at",
            ],
            "rationale": (
                "Needed to study next-day function or recommendation effects; these are outcomes, not same-night "
                "pre-20 predictors unless available before a later decision."
            ),
            "responses_collected_in_this_run": 0,
            "ranking_dimensions": ["independent_outcome", "temporal_overlap"],
        },
    ]


def serializable_panel(panel: pd.DataFrame) -> pd.DataFrame:
    output = panel.copy()
    for column in (
        "cutoff",
        "sleep_start",
        "sleep_end",
        "observed_at",
        "max_history_source_cutoff",
        "max_history_wake_at",
        "max_history_observed_at",
    ):
        output[column] = output[column].map(lambda value: "" if pd.isna(value) else value.isoformat())
    return output


def write_panel_sqlite_exclusive(path: Path, panel: pd.DataFrame) -> dict[str, Any]:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    connection = sqlite3.connect(path)
    try:
        panel.to_sql("analysis_panel", connection, index=False, if_exists="fail")
        connection.execute(
            "CREATE UNIQUE INDEX ux_analysis_panel_participant_cutoff "
            "ON analysis_panel(participant_id, cutoff)"
        )
        connection.commit()
        row_count = int(connection.execute("SELECT COUNT(*) FROM analysis_panel").fetchone()[0])
        unique_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM (SELECT participant_id, cutoff FROM analysis_panel "
                "GROUP BY participant_id, cutoff)"
            ).fetchone()[0]
        )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()
    if row_count != len(panel) or unique_count != len(panel) or integrity != "ok":
        raise ValueError("SQLite panel verification failed")
    return {"rows": row_count, "unique_participant_cutoffs": unique_count, "integrity_check": integrity}


def verify_selection_lock(
    path: Path,
    model_paths: dict[str, Path],
    expected_hashes: dict[str, str],
    expected_selected: str,
) -> dict[str, Pipeline]:
    if not path.is_file():
        raise FileNotFoundError("Selection lock must exist before TEST scoring")
    with path.open("r", encoding="utf-8") as handle:
        lock = json.load(handle)
    if lock.get("selected_arm") != expected_selected or not lock.get("locked_before_test_scoring"):
        raise ValueError("Selection lock does not match the validation decision")
    if lock.get("model_sha256") != expected_hashes:
        raise ValueError("Selection lock model hashes do not match")
    loaded: dict[str, Pipeline] = {}
    for arm, model_path in model_paths.items():
        if sha256_file(model_path) != expected_hashes[arm]:
            raise ValueError(f"Locked model changed before TEST scoring: {arm}")
        loaded[arm] = joblib.load(model_path)
    return loaded


def _hash_manifest(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    return {
        label: {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
        for label, path in paths.items()
    }


def run(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    output_dir = _resolve_workspace_file(workspace, EXPERIMENT_RELATIVE / "protocol.json").parent
    protocol_path = output_dir / "protocol.json"
    protocol = load_protocol(protocol_path)
    guard_outputs(output_dir)

    source_paths = {
        "protocol": protocol_path,
        "episodes": _resolve_workspace_file(workspace, protocol["sources"]["episodes"]),
        "sensor_cache": _resolve_workspace_file(workspace, protocol["sources"]["sensor_cache"]),
        "sensor_metadata": _resolve_workspace_file(workspace, protocol["sources"]["sensor_metadata"]),
        "night_core": _resolve_workspace_file(workspace, NIGHT_CORE_RELATIVE),
    }
    source_before = _hash_manifest(source_paths)
    with source_paths["sensor_metadata"].open("r", encoding="utf-8") as handle:
        sensor_metadata = json.load(handle)
    expected_cache_hash = sensor_metadata.get("artifact_sha256", {}).get("features_csv")
    actual_cache_hash = source_before["sensor_cache"]["sha256"]
    if not expected_cache_hash or actual_cache_hash != expected_cache_hash:
        raise ValueError(
            f"Sensor cache SHA-256 mismatch: metadata={expected_cache_hash}, actual={actual_cache_hash}"
        )
    metadata_episode_hash = sensor_metadata.get("source_sha256", {}).get("episodes")
    if metadata_episode_hash and source_before["episodes"]["sha256"] != metadata_episode_hash:
        raise ValueError("Episode CSV no longer matches the sensor-cache source hash")

    episodes = pd.read_csv(source_paths["episodes"])
    targets = prepare_p01_episodes(episodes)
    history = derive_history_features(targets)
    calendar = derive_calendar_features(targets)
    panel = pd.concat([targets, history, calendar], axis=1)

    cache = pd.read_csv(source_paths["sensor_cache"])
    if len(cache) != int(sensor_metadata.get("rows", -1)) or len(cache) != 196:
        raise ValueError("Sensor cache row count does not match metadata/frozen panel")
    cache["cutoff"] = _parse_kst_series(cache["cutoff"], "sensor cutoff")
    sensor_values = derive_sensor_features(cache)
    cache_for_join = pd.concat([cache[["cutoff"]], sensor_values], axis=1)
    panel = join_sensor_cache(panel, cache_for_join)
    verify_history_availability(panel)
    if panel["participant_id"].nunique() != 1 or panel["participant_id"].iloc[0] != PARTICIPANT_ID:
        raise ValueError("Analysis panel must contain privacy ID P01 only")

    density = split_density_summary(panel)
    verify_density_audit(density)
    associations = compute_associations(panel, protocol)
    models = fit_models(panel)
    model_paths, model_hashes = persist_models_exclusive(models, output_dir)
    constrain = load_night_core_constrain(source_paths["night_core"])
    prior = training_prior(panel)

    validation_predictions = predict_split(
        panel,
        "validation",
        models,
        model_hashes,
        constrain,
        prior,
        "validation_selection_only",
    )
    selection = select_sensor_arm(validation_predictions)
    selection_lock = {
        **selection,
        "schema_version": 1,
        "protocol_sha256": source_before["protocol"]["sha256"],
        "model_sha256": model_hashes,
        "model_features": {arm: list(features) for arm, features in ARM_FEATURES.items()},
        "model_protocol": {
            "imputation": "median_keep_empty_features_add_indicator",
            "scaling": "standard",
            "estimator": "multi_output_ridge",
            "ridge_alpha": RIDGE_ALPHA,
            "refit_on_validation": False,
            "hyperparameter_search": False,
        },
        "locked_before_test_scoring": True,
    }
    selection_path = output_dir / "selection.json"
    write_json_exclusive(selection_path, selection_lock)

    locked_models = verify_selection_lock(
        selection_path,
        model_paths,
        model_hashes,
        selection["selected_arm"],
    )
    test_predictions = predict_split(
        panel,
        "test",
        locked_models,
        model_hashes,
        constrain,
        prior,
        "test_scored_after_persisted_validation_lock",
    )
    predictions = pd.concat([validation_predictions, test_predictions], ignore_index=True)
    summaries = metrics_by_split(predictions)
    paired_bootstrap = test_paired_gains(predictions, selection["selected_arm"])

    panel_output = serializable_panel(panel)
    predictions_output = predictions.copy()
    predictions_output["cutoff"] = predictions_output["cutoff"].map(lambda value: value.isoformat())
    write_csv_exclusive(output_dir / "panel.csv", panel_output)
    sqlite_verification = write_panel_sqlite_exclusive(output_dir / "panel.sqlite", panel_output)
    write_csv_exclusive(output_dir / "association.csv", associations)
    write_csv_exclusive(output_dir / "predictions.csv", predictions_output)

    metrics = {
        "schema_version": 1,
        "design_status": protocol["design_status"],
        "cohort": {
            "participant_id": PARTICIPANT_ID,
            "n_nights": len(panel),
            "split_counts": EXPECTED_SPLITS,
            "single_person": True,
        },
        "data_expansion": {
            "genuinely_new_raw_measurements": 0,
            "derived_enrichment_feature_count": len(HISTORY_FEATURES) + len(SENSOR_FEATURES) + len(COVERAGE_FEATURES),
            "derived_enrichment_features": [*HISTORY_FEATURES, *SENSOR_FEATURES, *COVERAGE_FEATURES],
            "late_surveys_ingested": 0,
            "source_measurements_reused": True,
        },
        "density_and_coverage_by_split": density,
        "validation_metrics": summaries["validation"],
        "selection": selection,
        "test_metrics_scored_after_lock": summaries["test"],
        "test_same_night_calendar_week_cluster_bootstrap": paired_bootstrap,
        "association": {
            "selection_data": "train_only",
            "hypotheses": 12,
            "bh_family_size": 12,
            "train_minimum_complete_rows": ASSOCIATION_TRAIN_MIN_N,
            "validation_direction_minimum_complete_rows": ASSOCIATION_VALIDATION_MIN_N,
            "validation_direction_is_descriptive_only": True,
            "test_used": False,
            "cyclic_null": "all nonzero residual rotations with finite-sample p",
            "stationarity_caveat": (
                "The cyclic-shift null assumes approximately stationary ordered residuals; irregular calendar "
                "gaps and strong split drift make its p-values exploratory."
            ),
        },
        "sensor_semantics": {
            "hr_std": "same-day pre-20 heart-rate dispersion, not HRV",
            "coverage_predictors": (
                "log1p completed/exported HR and step-bin counts; availability proxies are not physiology"
            ),
            "step_bins": (
                "actual populated JSON export-bin counts (not fillna(0)); a median of 120 in every split does "
                "not demonstrate watch wearing, active sensing, or measurement quality"
            ),
            "zero_steps": (
                "zero is retained only when at least one step export bin exists; no export bins yields missing, "
                "not zero activity"
            ),
            "current_physiology_claim": False,
        },
        "prior_work_context": {
            "first_sensor_incorporation_claim": False,
            "prior_p01_joint_mae_min": {"data": 159.83, "data_sensors": 160.09},
            "interpretation": "This is a coverage-adjusted fixed-hypothesis follow-up, not the first sensor model.",
        },
        "collection_priorities": collection_priorities(density, selection),
        "limitations": [
            "Exploratory post-hoc analysis of one person whose TEST data had been inspected previously.",
            "TRAIN, validation, and TEST cover 227, 269, and 717 calendar days with strong temporal/domain drift.",
            "Sparse irregular gaps violate a simple exchangeable-night interpretation and only approximate the cyclic stationarity assumption.",
            "Historical sensor receipt/revision times are unknown; pre-20 availability is retrospective.",
            "Consumer-device onset/wake boundaries are not PSG total sleep time, required sleep, or independent physiology.",
            "Associations do not establish causal physiology, and bootstrap intervals are not population significance claims.",
            "No recommendation effect or product promotion is evaluated.",
        ],
    }
    write_json_exclusive(output_dir / "metrics.json", metrics)

    source_after = _hash_manifest(source_paths)
    sources_unchanged = all(
        source_before[label]["sha256"] == source_after[label]["sha256"]
        and source_before[label]["size_bytes"] == source_after[label]["size_bytes"]
        for label in source_paths
    )
    if not sources_unchanged:
        raise RuntimeError("A source changed during the run; results are not valid")
    source_hash_payload = {
        "schema_version": 1,
        "algorithm": "sha256",
        "before": source_before,
        "after": source_after,
        "all_sources_unchanged": sources_unchanged,
        "sensor_cache_metadata_expected_sha256": expected_cache_hash,
        "sensor_cache_verified_before_use": actual_cache_hash == expected_cache_hash,
    }
    write_json_exclusive(output_dir / "source_hashes.json", source_hash_payload)

    verification = {
        "schema_version": 1,
        "checks": {
            "protocol_read_from_experiment_folder": True,
            "output_guard_checked_all_planned_names_before_computation": True,
            "source_hashes_unchanged_before_after": sources_unchanged,
            "sensor_cache_csv_sha_matches_metadata": actual_cache_hash == expected_cache_hash,
            "panel_rows_196": len(panel) == 196,
            "split_137_29_30_preserved": panel["split"].value_counts().to_dict() == EXPECTED_SPLITS,
            "cutoff_join_one_to_one_complete": len(panel) == len(cache) and not panel["cutoff"].duplicated().any(),
            "history_cutoff_wake_observed_all_strictly_past": True,
            "privacy_id_only_in_panel": set(panel["participant_id"]) == {PARTICIPANT_ID},
            "no_source_paths_in_panel_rows": not any("path" in column.lower() for column in panel.columns),
            "late_surveys_not_loaded": True,
            "four_fixed_sensor_hypotheses": tuple(protocol["hypotheses"][index]["feature"] for index in range(4)) == SENSOR_FEATURES,
            "association_fixed_family_12": len(associations) == 12,
            "ridge_train_only_no_validation_refit": True,
            "selection_persisted_before_test_scoring": True,
            "test_predictions_loaded_from_hash_verified_models": True,
            "test_scored_same_nights_all_arms": all(
                len(predictions.loc[(predictions["split"] == "test") & (predictions["arm"] == arm)]) == 30
                for arm in PREDICTION_ARMS
            ),
            "bootstrap_seed_and_resamples_fixed": all(
                item["seed"] == BOOTSTRAP_SEED and item["resamples"] == BOOTSTRAP_RESAMPLES
                for item in paired_bootstrap
            ),
            "new_raw_measurements_zero": True,
            "sqlite_unique_p01_cutoff": sqlite_verification["unique_participant_cutoffs"] == 196,
            "no_upload_or_product_promotion": True,
        },
        "sqlite": sqlite_verification,
        "selection_lock_sha256": sha256_file(selection_path),
        "model_sha256": model_hashes,
        "verification_scope": "runtime invariants; unit tests are separate and are not executed by this script",
    }
    if not all(verification["checks"].values()):
        raise RuntimeError("One or more runtime verification checks failed")
    write_json_exclusive(output_dir / "verification.json", verification)

    hashed_outputs = {
        name: output_dir / name
        for name in OUTPUT_FILENAMES
        if name != "audit.json"
    }
    hashed_outputs.update({f"models/{MODEL_FILENAMES[arm]}": path for arm, path in model_paths.items()})
    audit = {
        "schema_version": 1,
        "artifact_type": "local_single_participant_exploratory_research",
        "participant_ids": [PARTICIPANT_ID],
        "privacy_names_in_rows": False,
        "source_paths_local_provenance_only": {label: str(path) for label, path in source_paths.items()},
        "output_sha256": {label: sha256_file(path) for label, path in hashed_outputs.items()},
        "exclusive_create": True,
        "original_snapshots_overwritten": False,
        "uploads": 0,
        "product_model_promoted": False,
        "selection_lock_sha256": sha256_file(selection_path),
        "source_hash_manifest_sha256": sha256_file(output_dir / "source_hashes.json"),
        "notes": [
            "No participant contact or new raw measurement occurred.",
            "Paths are retained only in this local provenance JSON, never in panel/SQLite rows.",
            "TEST results are exploratory because this person's TEST data were previously exposed.",
        ],
    }
    write_json_exclusive(output_dir / "audit.json", audit)
    return {
        "output_dir": str(output_dir),
        "selected_arm": selection["selected_arm"],
        "selection_lock_sha256": sha256_file(selection_path),
        "audit_sha256": sha256_file(output_dir / "audit.json"),
        "outputs": [*OUTPUT_FILENAMES, *[f"models/{name}" for name in MODEL_FILENAMES.values()]],
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path, help="Workspace root, e.g. E:/학술제")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = run(args.workspace)
    print(json.dumps(_json_ready(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
