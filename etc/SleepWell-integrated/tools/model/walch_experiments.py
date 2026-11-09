"""Leakage-resistant Walch sleep/wake experiments.

Raw PSG annotations are the only label source. Sensor features come from the
existing raw-derived 30-second cache, whose cached labels are deliberately
ignored. All splitting and scoring is performed at subject level.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

SEED = 20261010
NATIVE_EPOCH_SECONDS = 30
EPOCH_SECONDS = (30, 60, 120, 300)
TRAIN_ROW_CAP = 100_000
HGB_MAX_ITER = 60
MODEL_ORDER = ("dummy", "activity_logistic", "hr_activity_logistic", "hist_gradient_boosting")

ACTIVITY_FEATURES = ("activity_mean", "activity_std", "activity_n")
HR_FEATURES = ("hr_mean", "hr_std", "hr_n")
AGG_ACTIVITY_FEATURES = (
    "activity_mean_mean",
    "activity_mean_std",
    "activity_std_mean",
    "activity_n_sum",
)
AGG_HR_FEATURES = ("hr_mean_mean", "hr_mean_std", "hr_std_mean", "hr_n_sum")


@dataclass(frozen=True)
class Protocol:
    seed: int = SEED
    subject_split: str = "seeded_subject_shuffle_approximately_70_15_15"
    selection_metric: str = "validation_subject_macro_balanced_accuracy"
    epoch_seconds: tuple[int, ...] = EPOCH_SECONDS
    native_evaluation_seconds: int = NATIVE_EPOCH_SECONDS
    train_row_cap: int = TRAIN_ROW_CAP
    hgb_max_iter: int = HGB_MAX_ITER
    winner_rule: str = "maximum_validation_metric_then_model_order_then_shorter_latency"
    labels: str = "raw_PSG_wake_0_sleep_1_to_5_unscored_minus1_excluded"
    feature_timing: str = "current_nonoverlapping_bin_only_prediction_available_at_bin_end"
    prohibited_features: str = "no_PSG_stage_label_relative_time_centered_window_or_future_interpolation"
    common_support: str = "all_models_and_resolutions_scored_on_intersection_of_eligible_native30s_rows"
    hgb_early_stopping: bool = False
    revision: str = "v2_protocol_correction"
    supersedes: str = "walch_run_20261010"
    test_status: str = "exploratory_repeat_after_prior_v1_test_inspection_not_independent_or_pristine"


def _json_dump(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def sha256_file(path: Path, chunk_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def file_provenance(path: Path) -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(path),
    }


def subject_split(subject_ids: Iterable[str], seed: int = SEED) -> dict[str, list[str]]:
    ids = sorted({str(value) for value in subject_ids})
    if len(ids) < 7:
        raise ValueError("At least seven subjects are required for train/validation/test splitting")
    rng = np.random.default_rng(seed)
    shuffled = np.asarray(ids, dtype=object)
    rng.shuffle(shuffled)
    n = len(ids)
    n_train = int(round(n * 0.70))
    n_validation = int(round(n * 0.15))
    n_train = min(max(n_train, 1), n - 2)
    n_validation = min(max(n_validation, 1), n - n_train - 1)
    return {
        "train": sorted(shuffled[:n_train].tolist()),
        "validation": sorted(shuffled[n_train:n_train + n_validation].tolist()),
        "test": sorted(shuffled[n_train + n_validation:].tolist()),
    }


def _read_raw_labels(path: Path, subject_id: str) -> pd.DataFrame:
    labels = pd.read_csv(path, usecols=["epoch", "label"])
    if labels["epoch"].isna().any() or labels["label"].isna().any():
        raise ValueError(f"{subject_id}: raw labels contain missing epoch/label")
    epoch = pd.to_numeric(labels["epoch"], errors="raise")
    stage = pd.to_numeric(labels["label"], errors="raise")
    if not np.isfinite(epoch.to_numpy(dtype=float)).all() or not np.isfinite(stage.to_numpy(dtype=float)).all():
        raise ValueError(f"{subject_id}: raw labels contain non-finite values")
    if not np.all(np.equal(stage, np.floor(stage))):
        raise ValueError(f"{subject_id}: PSG stages must be integers")
    if not np.all(np.isclose(epoch % NATIVE_EPOCH_SECONDS, 0)):
        raise ValueError(f"{subject_id}: labels are not aligned to 30-second epochs")
    labels = pd.DataFrame({
        "subject_id": subject_id,
        "epoch_idx": (epoch // NATIVE_EPOCH_SECONDS).astype("int64"),
        "stage": stage.astype("int8"),
    })
    if labels["epoch_idx"].duplicated().any():
        raise ValueError(f"{subject_id}: duplicate raw PSG epochs")
    if not labels["stage"].isin([-1, 0, 1, 2, 3, 4, 5]).all():
        raise ValueError(f"{subject_id}: unexpected PSG stage")
    labels = labels[labels["stage"] != -1].copy()
    labels["is_sleep"] = (labels["stage"] != 0).astype("int8")
    return labels


def _read_cached_features(path: Path, subject_id: str) -> pd.DataFrame:
    usecols = ["subject_id", "epoch_idx", "t_rel_sec", *ACTIVITY_FEATURES, *HR_FEATURES]
    features = pd.read_csv(path, usecols=usecols)
    if len(features) == 0:
        raise ValueError(f"{subject_id}: empty feature cache")
    if features["epoch_idx"].duplicated().any():
        raise ValueError(f"{subject_id}: duplicate cached sensor epochs")
    if not (features["subject_id"].astype(str) == subject_id).all():
        raise ValueError(f"{subject_id}: cache subject mismatch")
    if not np.array_equal(
        features["t_rel_sec"].to_numpy(),
        features["epoch_idx"].to_numpy() * NATIVE_EPOCH_SECONDS,
    ):
        raise ValueError(f"{subject_id}: cache time grid mismatch")
    for column in ["epoch_idx", *ACTIVITY_FEATURES, *HR_FEATURES]:
        numeric = pd.to_numeric(features[column], errors="raise")
        observed = numeric.dropna().to_numpy(dtype=float)
        if not np.isfinite(observed).all():
            raise ValueError(f"{subject_id}: non-finite cached sensor value in {column}")
        features[column] = numeric
    return features.drop(columns=["subject_id", "t_rel_sec"])


def build_native_dataset(raw_root: Path, preprocessed_30s: Path) -> tuple[pd.DataFrame, dict]:
    """Join raw PSG truth to cached raw-sensor aggregates, never cached labels."""
    label_paths = sorted((raw_root / "labels").glob("*_labeled_sleep.csv"))
    if not label_paths:
        raise FileNotFoundError(f"No raw Walch labels under {raw_root / 'labels'}")
    frames = []
    audit_subjects = []
    source_files = []
    for label_path in label_paths:
        subject_id = label_path.name.split("_", 1)[0]
        feature_path = preprocessed_30s / f"{subject_id}_unified_30s.csv"
        if not feature_path.is_file():
            raise FileNotFoundError(f"Missing preprocessed sensor cache for {subject_id}")
        labels = _read_raw_labels(label_path, subject_id)
        features = _read_cached_features(feature_path, subject_id)
        merged = labels.merge(features, on="epoch_idx", how="left", validate="one_to_one")
        frames.append(merged)
        source_files.append({
            "subject_id": subject_id,
            "raw_label": file_provenance(label_path),
            "sensor_cache": file_provenance(feature_path),
        })
        audit_subjects.append({
            "subject_id": subject_id,
            "raw_scored_epochs": int(len(labels)),
            "activity_complete_epochs": int(merged[list(ACTIVITY_FEATURES)].notna().all(axis=1).sum()),
            "hr_complete_epochs": int(merged[list(HR_FEATURES)].notna().all(axis=1).sum()),
        })
    native = pd.concat(frames, ignore_index=True).sort_values(["subject_id", "epoch_idx"])
    audit = {
        "subjects": audit_subjects,
        "subject_count": len(audit_subjects),
        "native_scored_epochs": int(len(native)),
        "ground_truth_source": "raw labels/*_labeled_sleep.csv",
        "sensor_source": "preprocessed 30s cache reconstructed from raw motion/heart_rate",
        "cached_label_columns_used": False,
        "interpolation": "none",
        "centered_windows": False,
        "label_conditioned_features": False,
        "source_files": source_files,
    }
    return native.reset_index(drop=True), audit


def verify_one_subject_raw_motion(raw_root: Path, preprocessed_30s: Path) -> dict:
    """Bounded independent recomputation of the smallest raw motion file."""
    candidates = sorted((raw_root / "motion").glob("*_acceleration.csv"))
    if not candidates:
        raise FileNotFoundError(f"No raw motion files under {raw_root / 'motion'}")
    path = min(candidates, key=lambda item: (item.stat().st_size, item.name))
    subject_id = path.name.split("_", 1)[0]
    parts = []
    for chunk in pd.read_csv(path, chunksize=500_000):
        values = chunk[["timestamp", "x", "y", "z"]].apply(pd.to_numeric, errors="raise")
        if not np.isfinite(values.to_numpy(dtype=float)).all():
            raise ValueError(f"{subject_id}: non-finite raw motion value")
        x = values["x"].to_numpy(dtype=np.float32).astype(float)
        y = values["y"].to_numpy(dtype=np.float32).astype(float)
        z = values["z"].to_numpy(dtype=np.float32).astype(float)
        magnitude = np.sqrt(x ** 2 + y ** 2 + z ** 2)
        epoch_idx = np.floor_divide(values["timestamp"].to_numpy(dtype=float), NATIVE_EPOCH_SECONDS).astype("int64")
        part = pd.DataFrame({"epoch_idx": epoch_idx, "sum": magnitude, "sumsq": magnitude ** 2, "n": 1})
        parts.append(part.groupby("epoch_idx", as_index=False).sum())
    raw = pd.concat(parts).groupby("epoch_idx", as_index=False).sum()
    raw["activity_mean_raw"] = raw["sum"] / raw["n"]
    raw["activity_std_raw"] = np.sqrt(np.clip(raw["sumsq"] / raw["n"] - raw["activity_mean_raw"] ** 2, 0, None))
    cached = pd.read_csv(
        preprocessed_30s / f"{subject_id}_unified_30s.csv",
        usecols=["epoch_idx", "activity_mean", "activity_std", "activity_n"],
    )
    compared = raw.merge(cached, on="epoch_idx", how="inner", validate="one_to_one").dropna()
    if compared.empty:
        raise ValueError(f"{subject_id}: no overlapping raw/cache motion epochs")
    mean_error = np.abs(compared["activity_mean_raw"] - compared["activity_mean"])
    std_error = np.abs(compared["activity_std_raw"] - compared["activity_std"])
    count_match = compared["n"].astype("int64").eq(compared["activity_n"].astype("int64"))
    tolerance = 1e-6
    passed = bool(count_match.all() and mean_error.max() <= tolerance and std_error.max() <= tolerance)
    if not passed:
        raise ValueError(f"{subject_id}: raw motion recomputation differs from cache")
    return {
        "subject_id": subject_id,
        "selection": "smallest_raw_motion_file_then_name",
        "raw_file": file_provenance(path),
        "compared_epochs": int(len(compared)),
        "activity_count_exact_match": True,
        "activity_mean_max_abs_error": float(mean_error.max()),
        "activity_std_max_abs_error": float(std_error.max()),
        "absolute_tolerance": tolerance,
        "passed": True,
    }


def aggregate_epoch_dataset(native: pd.DataFrame, epoch_seconds: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create complete non-overlapping bins and the native-row projection map."""
    if epoch_seconds not in EPOCH_SECONDS:
        raise ValueError(f"Unsupported epoch_seconds={epoch_seconds}")
    factor = epoch_seconds // NATIVE_EPOCH_SECONDS
    required = {"subject_id", "epoch_idx", "is_sleep", *ACTIVITY_FEATURES, *HR_FEATURES}
    if not required.issubset(native.columns):
        raise ValueError(f"Missing columns: {sorted(required - set(native.columns))}")
    work = native.copy()
    work["bin_idx"] = np.floor_divide(work["epoch_idx"].to_numpy(), factor)
    work["complete_sensor"] = work[list(ACTIVITY_FEATURES + HR_FEATURES)].notna().all(axis=1)
    rows = []
    projection_rows = []
    for (sid, bin_idx), group in work.groupby(["subject_id", "bin_idx"], sort=True):
        ordered = group.sort_values("epoch_idx")
        expected = np.arange(int(bin_idx) * factor, int(bin_idx) * factor + factor)
        if len(ordered) != factor or not np.array_equal(ordered["epoch_idx"].to_numpy(), expected):
            continue
        if not ordered["complete_sensor"].all():
            continue
        activity = ordered["activity_mean"].to_numpy(dtype=float)
        hr = ordered["hr_mean"].to_numpy(dtype=float)
        y = ordered["is_sleep"].to_numpy(dtype=np.int8)
        rows.append({
            "subject_id": str(sid),
            "bin_idx": int(bin_idx),
            "epoch_seconds": epoch_seconds,
            "is_sleep": int(y.mean() >= 0.5),
            "activity_mean_mean": float(activity.mean()),
            "activity_mean_std": float(activity.std(ddof=0)),
            "activity_std_mean": float(ordered["activity_std"].mean()),
            "activity_n_sum": float(ordered["activity_n"].sum()),
            "hr_mean_mean": float(hr.mean()),
            "hr_mean_std": float(hr.std(ddof=0)),
            "hr_std_mean": float(ordered["hr_std"].mean()),
            "hr_n_sum": float(ordered["hr_n"].sum()),
        })
        projection_rows.extend(
            {"subject_id": str(sid), "bin_idx": int(bin_idx), "epoch_idx": int(e), "y_true": int(t)}
            for e, t in zip(ordered["epoch_idx"], y)
        )
    aggregate = pd.DataFrame(rows)
    projection = pd.DataFrame(projection_rows)
    if aggregate.empty:
        raise ValueError(f"No complete {epoch_seconds}-second bins")
    numeric = aggregate[["is_sleep", *AGG_ACTIVITY_FEATURES, *AGG_HR_FEATURES]].to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise ValueError(f"Non-finite aggregate feature at {epoch_seconds} seconds")
    return aggregate, projection


def make_model(name: str):
    if name == "dummy":
        return DummyClassifier(strategy="prior")
    if name == "activity_logistic":
        features = list(AGG_ACTIVITY_FEATURES)
    elif name == "hr_activity_logistic":
        features = list(AGG_ACTIVITY_FEATURES + AGG_HR_FEATURES)
    elif name == "hist_gradient_boosting":
        return HistGradientBoostingClassifier(
            learning_rate=0.08,
            max_iter=HGB_MAX_ITER,
            max_leaf_nodes=15,
            min_samples_leaf=20,
            l2_regularization=1.0,
            class_weight="balanced",
            early_stopping=False,
            random_state=SEED,
        ), list(AGG_ACTIVITY_FEATURES + AGG_HR_FEATURES)
    else:
        raise ValueError(f"Unknown model {name}")
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
        ("scale", StandardScaler()),
        ("model", LogisticRegression(class_weight="balanced", max_iter=1000, random_state=SEED)),
    ]), features


def _subject_balanced_cap(frame: pd.DataFrame, cap: int, seed: int) -> pd.DataFrame:
    if len(frame) <= cap:
        return frame.sort_values(["subject_id", "bin_idx"]).reset_index(drop=True)
    subjects = sorted(frame["subject_id"].unique())
    per_subject = max(cap // len(subjects), 1)
    parts = []
    for offset, sid in enumerate(subjects):
        part = frame[frame["subject_id"] == sid]
        if len(part) > per_subject:
            part = part.sample(per_subject, random_state=seed + offset)
        parts.append(part)
    sampled = pd.concat(parts)
    if len(sampled) > cap:
        sampled = sampled.sample(cap, random_state=seed)
    return sampled.sort_values(["subject_id", "bin_idx"]).reset_index(drop=True)


def _predict_probability(model, x: pd.DataFrame) -> np.ndarray:
    probability = model.predict_proba(x)
    classes = list(model.classes_)
    if 1 not in classes:
        return np.zeros(len(x), dtype=float)
    return probability[:, classes.index(1)]


def project_predictions(
    aggregate: pd.DataFrame,
    projection: pd.DataFrame,
    subject_ids: Iterable[str],
    probabilities: np.ndarray,
) -> pd.DataFrame:
    ids = {str(value) for value in subject_ids}
    selected = aggregate[aggregate["subject_id"].isin(ids)][["subject_id", "bin_idx"]].copy()
    if len(selected) != len(probabilities):
        raise ValueError("Prediction count does not match aggregate rows")
    selected["probability_sleep"] = probabilities
    selected["y_pred"] = (selected["probability_sleep"] >= 0.5).astype("int8")
    return projection.merge(selected, on=["subject_id", "bin_idx"], how="inner", validate="many_to_one")


def _metric_values(predictions: pd.DataFrame) -> dict[str, float | int | None]:
    y_true = predictions["y_true"]
    y_pred = predictions["y_pred"]
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    auc = None
    if y_true.nunique() == 2:
        auc = float(roc_auc_score(y_true, predictions["probability_sleep"]))
    return {
        "native_balanced_accuracy": float(balanced_accuracy_score(y_true, y_pred)),
        "native_accuracy": float(accuracy_score(y_true, y_pred)),
        "native_f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "native_macro_f1": float(f1_score(y_true, y_pred, average="macro", zero_division=0)),
        "wake_specificity": float(tn / (tn + fp)) if tn + fp else None,
        "sleep_sensitivity": float(tp / (tp + fn)) if tp + fn else None,
        "native_roc_auc": auc,
        "confusion": {"wake_as_wake_tn": int(tn), "wake_as_sleep_fp": int(fp),
                      "sleep_as_wake_fn": int(fn), "sleep_as_sleep_tp": int(tp)},
    }


def per_subject_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for subject_id, group in predictions.groupby("subject_id"):
        row = {"subject_id": subject_id, "native_rows": len(group), "one_class": group["y_true"].nunique() < 2}
        row.update(_metric_values(group))
        rows.append(row)
    return pd.DataFrame(rows)


def metrics(predictions: pd.DataFrame) -> dict:
    subject_metrics = per_subject_metrics(predictions)
    eligible = subject_metrics[~subject_metrics["one_class"]]
    result = {
        "native_rows": int(len(predictions)),
        "subjects": int(predictions["subject_id"].nunique()),
        "subjects_excluded_one_class_from_macro": int(subject_metrics["one_class"].sum()),
        "subject_macro_balanced_accuracy": (
            float(eligible["native_balanced_accuracy"].mean()) if len(eligible) else None
        ),
        "subject_macro_f1": float(eligible["native_macro_f1"].mean()) if len(eligible) else None,
    }
    result.update(_metric_values(predictions))
    return result


def paired_subject_bootstrap(
    winner_predictions: pd.DataFrame,
    dummy_predictions: pd.DataFrame,
    iterations: int = 2000,
    seed: int = SEED,
) -> dict:
    winner = per_subject_metrics(winner_predictions).set_index("subject_id")
    dummy = per_subject_metrics(dummy_predictions).set_index("subject_id")
    common = winner.index.intersection(dummy.index)
    paired = pd.DataFrame({
        "winner": winner.loc[common, "native_balanced_accuracy"],
        "dummy": dummy.loc[common, "native_balanced_accuracy"],
    }).dropna()
    differences = (paired["winner"] - paired["dummy"]).to_numpy(dtype=float)
    if len(differences) == 0:
        raise ValueError("No paired subjects for bootstrap")
    rng = np.random.default_rng(seed)
    sampled = rng.choice(differences, size=(iterations, len(differences)), replace=True).mean(axis=1)
    low, high = np.quantile(sampled, [0.025, 0.975])
    return {
        "metric": "paired_subject_balanced_accuracy_difference_winner_minus_dummy",
        "subjects": int(len(differences)),
        "iterations": iterations,
        "seed": seed,
        "point_difference": float(differences.mean()),
        "percentile_95_ci": [float(low), float(high)],
        "caution": "Only four held-out subjects were planned; this interval is highly unstable and descriptive.",
    }


def choose_winner(validation_results: list[dict]) -> dict:
    order = {name: index for index, name in enumerate(MODEL_ORDER)}
    eligible = [row for row in validation_results if row["metrics"]["subject_macro_balanced_accuracy"] is not None]
    if not eligible:
        raise ValueError("No eligible validation results")
    return max(
        eligible,
        key=lambda row: (
            row["metrics"]["subject_macro_balanced_accuracy"],
            -order[row["model"]],
            -row["epoch_seconds"],
        ),
    )


def common_native_support(prepared: dict[int, tuple[pd.DataFrame, pd.DataFrame]]) -> pd.DataFrame:
    supports = []
    for _, projection in prepared.values():
        supports.append(set(projection[["subject_id", "epoch_idx"]].itertuples(index=False, name=None)))
    common = set.intersection(*supports)
    if not common:
        raise ValueError("No common native 30-second support across epoch resolutions")
    return pd.DataFrame(sorted(common), columns=["subject_id", "epoch_idx"])


def restrict_projection(projection: pd.DataFrame, support: pd.DataFrame) -> pd.DataFrame:
    return projection.merge(support, on=["subject_id", "epoch_idx"], how="inner", validate="one_to_one")


def _hgb_validation_curve(model, x_validation: pd.DataFrame, aggregate_validation: pd.DataFrame,
                          projection: pd.DataFrame) -> list[dict]:
    curve = []
    for iteration, probabilities in enumerate(model.staged_predict_proba(x_validation), start=1):
        projected = project_predictions(
            aggregate_validation, projection, aggregate_validation["subject_id"].unique(), probabilities[:, 1]
        )
        score = metrics(projected)["subject_macro_balanced_accuracy"]
        curve.append({"boosting_iteration": iteration, "validation_subject_macro_balanced_accuracy": score})
    return curve


def run_experiments(raw_root: Path, preprocessed_30s: Path, output_dir: Path) -> dict:
    with threadpool_limits(limits=2):
        return _run_experiments(raw_root, preprocessed_30s, output_dir)


def _run_experiments(raw_root: Path, preprocessed_30s: Path, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=False)
    protocol = asdict(Protocol())
    protocol["epoch_seconds"] = list(protocol["epoch_seconds"])
    protocol["protocol_written_before_test_evaluation"] = True
    _json_dump(output_dir / "protocol.json", protocol)

    native, audit = build_native_dataset(raw_root, preprocessed_30s)
    preprocessor_code = preprocessed_30s.parent.parent / "scripts" / "walch_preprocess.py"
    if not preprocessor_code.is_file():
        raise FileNotFoundError(f"Missing source cache builder: {preprocessor_code}")
    provenance = {
        "experiment_code": file_provenance(Path(__file__).resolve()),
        "source_cache_builder_code": file_provenance(preprocessor_code),
        "source_cache_builder_path": str(preprocessor_code.resolve()),
        "source_cache_builder_contract": {
            "motion": "load_motion: floor raw timestamps to 30s; magnitude mean/std/count",
            "heart_rate": "load_heart_rate: floor raw timestamps to 30s; HR mean/std/count",
            "join": "build_unified_30s: outer 30s grid; experiment re-joins raw PSG and ignores cached labels",
        },
        "environment": {
            "python": sys.version, "platform": platform.platform(),
            "numpy": np.__version__, "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__, "joblib": joblib.__version__,
        },
        "bounded_raw_motion_recomputation": verify_one_subject_raw_motion(raw_root, preprocessed_30s),
        "source_files": audit.pop("source_files"),
    }
    splits = subject_split(native["subject_id"].unique())
    _json_dump(output_dir / "split_manifest_private.json", splits)
    public_split = {name: {"subjects": len(ids)} for name, ids in splits.items()}
    _json_dump(output_dir / "split_summary.json", public_split)
    _json_dump(output_dir / "preprocessing_audit_private.json", audit)
    _json_dump(output_dir / "provenance_private.json", provenance)
    native.to_csv(output_dir / "model_ready_native30s_private.csv.gz", index=False, compression="gzip")

    validation_results = []
    fitted = {}
    prepared = {}
    curves = []
    timings = []
    private_subject_metrics = []
    for epoch_seconds in EPOCH_SECONDS:
        aggregate, projection = aggregate_epoch_dataset(native, epoch_seconds)
        aggregate.to_csv(output_dir / f"model_ready_{epoch_seconds}s_private.csv.gz", index=False, compression="gzip")
        prepared[epoch_seconds] = (aggregate, projection)

    support = common_native_support(prepared)
    support.to_csv(output_dir / "evaluation_native30s_support_private.csv.gz", index=False, compression="gzip")
    complete_sensor = native[list(ACTIVITY_FEATURES + HR_FEATURES)].notna().all(axis=1)
    support_keys = pd.MultiIndex.from_frame(support[["subject_id", "epoch_idx"]])
    native_keys = pd.MultiIndex.from_frame(native[["subject_id", "epoch_idx"]])
    in_support = native_keys.isin(support_keys)
    coverage = {
        "raw_psg_scored_epochs": int(len(native)),
        "raw_psg_sleep_rate": float(native["is_sleep"].mean()),
        "complete_sensor_native_epochs": int(complete_sensor.sum()),
        "complete_sensor_coverage_of_scored_psg": float(complete_sensor.mean()),
        "cross_resolution_common_native_epochs": int(in_support.sum()),
        "common_support_coverage_of_scored_psg": float(in_support.mean()),
        "common_support_sleep_rate": float(native.loc[in_support, "is_sleep"].mean()),
        "by_split": {},
    }
    for split_name, ids in splits.items():
        split_mask = native["subject_id"].isin(ids)
        split_common = split_mask & in_support
        coverage["by_split"][split_name] = {
            "scored_psg_epochs": int(split_mask.sum()),
            "common_support_epochs": int(split_common.sum()),
            "common_support_sleep_rate": float(native.loc[split_common, "is_sleep"].mean()),
        }
    _json_dump(output_dir / "coverage_summary.json", coverage)
    for epoch_seconds in EPOCH_SECONDS:
        aggregate, projection = prepared[epoch_seconds]
        train = _subject_balanced_cap(
            aggregate[aggregate["subject_id"].isin(splits["train"])], TRAIN_ROW_CAP, SEED
        )
        validation = aggregate[aggregate["subject_id"].isin(splits["validation"])]
        validation_projection = restrict_projection(
            projection[projection["subject_id"].isin(splits["validation"])],
            support[support["subject_id"].isin(splits["validation"])],
        )
        for model_name in MODEL_ORDER:
            estimator, feature_names = make_model(model_name) if model_name != "dummy" else (make_model(model_name), list(AGG_ACTIVITY_FEATURES + AGG_HR_FEATURES))
            started = time.perf_counter()
            estimator.fit(train[feature_names], train["is_sleep"])
            fit_seconds = time.perf_counter() - started
            started = time.perf_counter()
            probability = _predict_probability(estimator, validation[feature_names])
            inference_seconds = time.perf_counter() - started
            projected = project_predictions(validation, validation_projection, splits["validation"], probability)
            result = {
                "split": "validation",
                "epoch_seconds": epoch_seconds,
                "latency_seconds": epoch_seconds,
                "model": model_name,
                "feature_names": feature_names,
                "train_rows": int(len(train)),
                "aggregate_evaluation_rows": int(len(validation)),
                "metrics": metrics(projected),
            }
            validation_results.append(result)
            subject_frame = per_subject_metrics(projected)
            subject_frame.insert(0, "model", model_name)
            subject_frame.insert(0, "epoch_seconds", epoch_seconds)
            subject_frame.insert(0, "split", "validation")
            private_subject_metrics.append(subject_frame)
            fitted[(epoch_seconds, model_name)] = (estimator, feature_names)
            timings.append({
                "epoch_seconds": epoch_seconds, "model": model_name,
                "fit_seconds": fit_seconds, "validation_inference_seconds": inference_seconds,
                "validation_native_rows": len(projected),
            })
            if model_name == "hist_gradient_boosting":
                for point in _hgb_validation_curve(
                    estimator, validation[feature_names], validation, validation_projection
                ):
                    curves.append({"epoch_seconds": epoch_seconds, **point})

    winner = choose_winner(validation_results)
    _json_dump(output_dir / "winner_frozen_from_validation.json", winner)

    # Test data are evaluated only after the validation-selected winner is frozen.
    test_results = []
    prediction_parts = []
    test_predictions_by_candidate = {}
    for epoch_seconds in EPOCH_SECONDS:
        aggregate, projection = prepared[epoch_seconds]
        test = aggregate[aggregate["subject_id"].isin(splits["test"])]
        test_projection = restrict_projection(
            projection[projection["subject_id"].isin(splits["test"])],
            support[support["subject_id"].isin(splits["test"])],
        )
        for model_name in MODEL_ORDER:
            estimator, feature_names = fitted[(epoch_seconds, model_name)]
            probability = _predict_probability(estimator, test[feature_names])
            projected = project_predictions(test, test_projection, splits["test"], probability)
            projected.insert(0, "model", model_name)
            projected.insert(0, "epoch_seconds", epoch_seconds)
            prediction_parts.append(projected)
            test_predictions_by_candidate[(epoch_seconds, model_name)] = projected
            test_results.append({
                "split": "test_descriptive_not_used_for_selection",
                "epoch_seconds": epoch_seconds,
                "latency_seconds": epoch_seconds,
                "model": model_name,
                "feature_names": feature_names,
                "aggregate_evaluation_rows": int(len(test)),
                "metrics": metrics(projected),
                "is_validation_selected_winner": (
                    epoch_seconds == winner["epoch_seconds"] and model_name == winner["model"]
                ),
            })
            subject_frame = per_subject_metrics(projected)
            subject_frame.insert(0, "model", model_name)
            subject_frame.insert(0, "epoch_seconds", epoch_seconds)
            subject_frame.insert(0, "split", "test_descriptive")
            private_subject_metrics.append(subject_frame)
    pd.concat(prediction_parts, ignore_index=True).to_csv(
        output_dir / "test_predictions_private.csv.gz", index=False, compression="gzip"
    )
    pd.DataFrame(curves).to_csv(output_dir / "hgb_validation_learning_curve.csv", index=False)
    pd.DataFrame(timings).to_csv(output_dir / "runtime_metrics.csv", index=False)
    pd.concat(private_subject_metrics, ignore_index=True).to_csv(
        output_dir / "per_subject_metrics_private.csv.gz", index=False, compression="gzip"
    )

    bootstrap = paired_subject_bootstrap(
        test_predictions_by_candidate[(winner["epoch_seconds"], winner["model"])],
        test_predictions_by_candidate[(winner["epoch_seconds"], "dummy")],
    )
    _json_dump(output_dir / "winner_vs_dummy_paired_bootstrap.json", bootstrap)

    selected_model, selected_features = fitted[(winner["epoch_seconds"], winner["model"])]
    artifact = {
        "research_only": True,
        "not_for_clinical_or_production_use": True,
        "dataset": "Walch Apple Watch with raw PSG labels",
        "seed": SEED,
        "epoch_seconds": winner["epoch_seconds"],
        "latency_seconds": winner["epoch_seconds"],
        "model": winner["model"],
        "feature_names": selected_features,
        "validation_metrics": winner["metrics"],
        "estimator": selected_model,
    }
    joblib.dump(artifact, output_dir / "selected_model_research_only.joblib")

    results = {
        "protocol": protocol,
        "dataset": {key: value for key, value in audit.items() if key != "subjects"},
        "coverage": coverage,
        "split_summary": public_split,
        "validation_results": validation_results,
        "validation_selected_winner": winner,
        "test_results_descriptive": test_results,
        "winner_vs_dummy_paired_bootstrap": bootstrap,
    }
    _json_dump(output_dir / "aggregate_metrics.json", results)
    _write_report(output_dir / "REPORT.md", results)
    return results


def _write_report(path: Path, results: dict) -> None:
    winner = results["validation_selected_winner"]
    matching_test = next(row for row in results["test_results_descriptive"] if row["is_validation_selected_winner"])
    coverage = results["coverage"]
    bootstrap = results["winner_vs_dummy_paired_bootstrap"]
    lines = [
        "# Walch PSG sleep/wake experiment",
        "",
        "Research-only result; not a clinical or production model.",
        "This v2 corrects HGB fixed-iteration behavior after v1 test inspection; its test result is an exploratory repeat, not an independent or pristine holdout.",
        "",
        "## Frozen design",
        "",
        f"- Subject split: {results['split_summary']}",
        "- Raw PSG annotations supply all targets; cached label columns are ignored.",
        "- Features use only sensor observations inside the current non-overlapping bin.",
        "- Predictions become available at bin end, so latency equals 30/60/120/300 seconds.",
        "- No centered/future interpolation, PSG-relative clock, or label-derived feature is used.",
        "- Every model and resolution is scored on the same intersection of eligible native 30-second rows.",
        "- HGB uses exactly 60 boosting iterations with early stopping disabled.",
        "",
        "## Coverage",
        "",
        f"- Raw scored PSG epochs: {coverage['raw_psg_scored_epochs']:,} "
        f"(sleep rate {coverage['raw_psg_sleep_rate']:.3f})",
        f"- Complete sensor epochs: {coverage['complete_sensor_native_epochs']:,} "
        f"({coverage['complete_sensor_coverage_of_scored_psg']:.1%} of scored PSG)",
        f"- Common cross-resolution support: {coverage['cross_resolution_common_native_epochs']:,} "
        f"({coverage['common_support_coverage_of_scored_psg']:.1%}; sleep rate "
        f"{coverage['common_support_sleep_rate']:.3f})",
        "",
        "## Validation-selected winner",
        "",
        f"- Model: `{winner['model']}` at {winner['epoch_seconds']} seconds",
        f"- Validation subject-macro balanced accuracy: {winner['metrics']['subject_macro_balanced_accuracy']:.4f}",
        f"- Held-out test subject-macro balanced accuracy: {matching_test['metrics']['subject_macro_balanced_accuracy']:.4f}",
        f"- Test macro F1: {matching_test['metrics']['native_macro_f1']:.4f}; "
        f"wake specificity: {matching_test['metrics']['wake_specificity']:.4f}; "
        f"sleep sensitivity: {matching_test['metrics']['sleep_sensitivity']:.4f}",
        f"- Winner minus dummy paired-subject difference: {bootstrap['point_difference']:.4f}, "
        f"95% bootstrap CI [{bootstrap['percentile_95_ci'][0]:.4f}, "
        f"{bootstrap['percentile_95_ci'][1]:.4f}] ({bootstrap['subjects']} subjects; descriptive)",
        "",
        "All test candidates are descriptive only; the winner was frozen from validation results first.",
        "HGB boosting iterations are optimization steps, distinct from the sensor epoch duration.",
        "",
        "## Method reference",
        "",
        "Kassis, T., Agarwal, V., He, Y., Patel, D., & Brueckner, A. M. (2026). "
        "Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents. "
        "arXiv:2609.00065. https://doi.org/10.48550/arXiv.2609.00065",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--preprocessed-30s", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
    os.environ.setdefault("MKL_NUM_THREADS", "2")
    run_experiments(args.raw_root, args.preprocessed_30s, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
