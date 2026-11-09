#!/usr/bin/env python3
"""Build and evaluate research-only MESA actigraphy weak-label models."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "2")

import joblib
import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


EPOCH_SECONDS = (30, 60, 120, 300)
FEATURE_NAMES = ("activity", "whitelight", "redlight", "greenlight", "bluelight")
SPLIT_SEED = "mesa-subject-split-v1"
TOKEN_NAMESPACE = "mesa_subject_v1"
MAX_PER_SUBJECT = 200
MAX_TRAIN_ROWS = 200_000
COMMON_BLOCK_ROWS = 20
EXPECTED_HEADER = {
    "mesaid", "line", "offwrist", "activity", "whitelight", "redlight",
    "greenlight", "bluelight", "wake", "interval",
}
CANDIDATES = (
    {"name": "dummy_prior", "features": ()},
    {"name": "activity_logistic", "features": ("activity",)},
    {"name": "activity_light_logistic", "features": FEATURE_NAMES},
    {"name": "activity_light_hgb", "features": FEATURE_NAMES},
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_participant_id(value: str) -> str:
    stripped = value.strip()
    if not stripped.isdigit():
        raise ValueError("participant ID is not numeric")
    return str(int(stripped))


def participant_from_name(path: Path) -> str | None:
    parts = path.stem.rsplit("-", 1)
    if len(parts) != 2 or parts[0].lower() != "mesa-sleep" or not parts[1].isdigit():
        return None
    return normalize_participant_id(parts[1])


def subject_token(participant: str) -> str:
    digest = hashlib.sha256(f"{TOKEN_NAMESPACE}|{normalize_participant_id(participant)}".encode()).hexdigest()
    return f"mesa_{digest[:20]}"


def subject_split(participant: str) -> str:
    digest = hashlib.sha256(f"{SPLIT_SEED}|{normalize_participant_id(participant)}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") % 100
    return "train" if bucket < 70 else "validation" if bucket < 85 else "test"


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns


class _HashingRaw(io.RawIOBase):
    def __init__(self, raw: io.RawIOBase, digest: "hashlib._Hash") -> None:
        self.raw = raw
        self.digest = digest

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        count = self.raw.readinto(buffer)
        if count:
            self.digest.update(memoryview(buffer)[:count])
        return count

    def close(self) -> None:
        if not self.closed:
            self.raw.close()
        super().close()


def _finite_sensor(value: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    limit = float(np.finfo(np.float32).max)
    return number if math.isfinite(number) and 0.0 <= number <= limit else None


def _shifted_timestamp(row: dict) -> datetime | None:
    date = (row.get("date_shifted") or "").strip()
    time = (row.get("linetime") or "").strip()
    if not date or not time:
        return None
    try:
        return datetime.fromisoformat(f"{date}T{time}")
    except ValueError:
        return None


def extract_common_rows(path: Path, expected_participant: str) -> tuple[dict, dict, str]:
    """Read one complete file and retain only fully valid ten-minute blocks."""
    features: list[list[float]] = []
    labels: list[int] = []
    lines: list[int] = []
    relative_seconds: list[int] = []
    qc: Counter[str] = Counter()
    current_block: int | None = None
    seen_lines: list[int] = []
    valid_rows: list[tuple[int, int, list[float], int]] = []
    digest = hashlib.sha256()
    first_timestamp: datetime | None = None
    previous_timestamp: datetime | None = None
    previous_line: int | None = None

    def finish_block() -> None:
        if current_block is None:
            return
        expected = list(range(current_block * COMMON_BLOCK_ROWS + 1, current_block * COMMON_BLOCK_ROWS + 1 + COMMON_BLOCK_ROWS))
        if seen_lines == expected and len(valid_rows) == COMMON_BLOCK_ROWS:
            for line, relative, row_features, label in valid_rows:
                lines.append(line)
                relative_seconds.append(relative)
                features.append(row_features)
                labels.append(label)
            qc["eligible_ten_minute_blocks"] += 1
        else:
            qc["excluded_incomplete_or_invalid_ten_minute_blocks"] += 1

    with path.open("rb", buffering=0) as binary:
        hashing_raw = _HashingRaw(binary, digest)
        with io.TextIOWrapper(io.BufferedReader(hashing_raw), encoding="utf-8-sig", newline="") as text:
            reader = csv.DictReader(text)
            header = set(reader.fieldnames or ())
            if not EXPECTED_HEADER.issubset(header):
                raise ValueError("required MESA actigraphy columns are missing")
            for row in reader:
                qc["source_rows"] += 1
                try:
                    line = int((row.get("line") or "").strip())
                except ValueError:
                    qc["invalid_line_rows"] += 1
                    continue
                if line < 1:
                    qc["invalid_line_rows"] += 1
                    continue
                timestamp = _shifted_timestamp(row)
                if timestamp is None:
                    raise ValueError("missing or invalid shifted timestamp")
                if previous_line is not None:
                    line_delta = line - previous_line
                    time_delta = int((timestamp - previous_timestamp).total_seconds())
                    if line_delta <= 0:
                        raise ValueError("line is not strictly increasing and unique")
                    if time_delta != line_delta * 30:
                        raise ValueError("line/time cadence is not consistently 30 seconds")
                if first_timestamp is None:
                    first_timestamp = timestamp
                relative = int((timestamp - first_timestamp).total_seconds())
                previous_line = line
                previous_timestamp = timestamp
                block = (line - 1) // COMMON_BLOCK_ROWS
                if current_block is None:
                    current_block = block
                elif block != current_block:
                    finish_block()
                    current_block = block
                    seen_lines = []
                    valid_rows = []
                seen_lines.append(line)

                try:
                    content_participant = normalize_participant_id(row.get("mesaid") or "")
                except ValueError:
                    qc["invalid_participant_rows"] += 1
                    continue
                if content_participant != expected_participant:
                    qc["participant_mismatch_rows"] += 1
                    continue
                offwrist = (row.get("offwrist") or "").strip()
                if offwrist != "0":
                    qc["offwrist_or_invalid_rows"] += 1
                    continue
                wake = (row.get("wake") or "").strip()
                if wake not in {"0", "1"}:
                    qc["missing_or_invalid_wake_rows"] += 1
                    continue
                values = [_finite_sensor(row.get(name) or "") for name in FEATURE_NAMES]
                if any(value is None for value in values):
                    qc["missing_or_invalid_sensor_rows"] += 1
                    continue
                valid_rows.append((line, relative, [float(value) for value in values], 1 - int(wake)))
            finish_block()

    arrays = {
        "line": np.asarray(lines, dtype=np.int32),
        "t_rel_sec": np.asarray(relative_seconds, dtype=np.int64),
        "x": np.asarray(features, dtype=np.float32).reshape((-1, len(FEATURE_NAMES))),
        "y_sleep": np.asarray(labels, dtype=np.uint8),
    }
    qc["eligible_native_rows"] = len(lines)
    return arrays, dict(sorted(qc.items())), digest.hexdigest()


def aggregate_epoch(arrays: dict, epoch_seconds: int) -> dict:
    if epoch_seconds not in EPOCH_SECONDS:
        raise ValueError("unsupported epoch")
    rows_per_epoch = epoch_seconds // 30
    lines = arrays["line"]
    x = arrays["x"]
    y = arrays["y_sleep"]
    if not len(lines):
        return {
            "line_start": np.empty(0, dtype=np.int32),
            "x": np.empty((0, len(FEATURE_NAMES)), dtype=np.float32),
            "y": np.empty(0, dtype=np.uint8),
        }
    bins = (lines - 1) // rows_per_epoch
    changes = np.flatnonzero(np.diff(bins)) + 1
    starts = np.r_[0, changes]
    ends = np.r_[changes, len(lines)]
    counts = ends - starts
    expected_starts = bins[starts].astype(np.int64) * rows_per_epoch + 1
    complete = (
        (counts == rows_per_epoch)
        & (lines[starts] == expected_starts)
        & (lines[ends - 1] == expected_starts + rows_per_epoch - 1)
    )
    sums_x = np.add.reduceat(x.astype(np.float64), starts, axis=0)
    sums_y = np.add.reduceat(y.astype(np.int64), starts)
    output_lines = expected_starts[complete].astype(np.int32)
    output_x = (sums_x[complete] / rows_per_epoch).astype(np.float32)
    output_y = (sums_y[complete] * 2 >= rows_per_epoch).astype(np.uint8)
    return {
        "line_start": output_lines,
        "x": output_x.reshape((-1, len(FEATURE_NAMES))),
        "y": output_y,
    }


def _even_indices(length: int, maximum: int) -> np.ndarray:
    if length <= maximum:
        return np.arange(length, dtype=np.int64)
    return np.linspace(0, length - 1, maximum, dtype=np.int64)


def _feature_indices(names: Iterable[str]) -> list[int]:
    return [FEATURE_NAMES.index(name) for name in names]


def _make_model(candidate: dict):
    name = candidate["name"]
    if name == "dummy_prior":
        return DummyClassifier(strategy="prior")
    if name.endswith("logistic"):
        return make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=300, class_weight="balanced", random_state=42),
        )
    if name == "activity_light_hgb":
        return HistGradientBoostingClassifier(
            max_iter=60,
            max_leaf_nodes=15,
            learning_rate=0.08,
            min_samples_leaf=20,
            l2_regularization=1.0,
            class_weight="balanced",
            early_stopping=False,
            random_state=42,
        )
    raise ValueError(f"unknown candidate: {name}")


def _candidate_x(x: np.ndarray, candidate: dict) -> np.ndarray:
    indices = _feature_indices(candidate["features"])
    return x[:, indices] if indices else np.zeros((len(x), 1), dtype=np.float32)


def _load_subject(path: Path) -> dict:
    with np.load(path, allow_pickle=False) as loaded:
        return {name: loaded[name] for name in ("line", "t_rel_sec", "x", "y_sleep")}


def collect_training(subjects: list[dict], subject_dir: Path, epoch_seconds: int) -> tuple[np.ndarray, np.ndarray, dict]:
    x_parts: list[np.ndarray] = []
    y_parts: list[np.ndarray] = []
    rows = 0
    included_subjects = 0
    per_subject_budget = max(1, min(MAX_PER_SUBJECT, MAX_TRAIN_ROWS // max(1, len(subjects))))
    for subject in subjects:
        epoch = aggregate_epoch(_load_subject(subject_dir / f"{subject['token']}.npz"), epoch_seconds)
        if not len(epoch["y"]):
            continue
        indices = _even_indices(len(epoch["y"]), per_subject_budget)
        x_parts.append(epoch["x"][indices])
        y_parts.append(epoch["y"][indices])
        rows += len(indices)
        included_subjects += 1
    if not x_parts:
        raise ValueError("no eligible training rows")
    return np.concatenate(x_parts), np.concatenate(y_parts), {
        "sampled_rows": rows,
        "subjects": included_subjects,
        "configured_max_rows_per_subject": MAX_PER_SUBJECT,
        "effective_rows_per_subject_budget": per_subject_budget,
        "global_cap": MAX_TRAIN_ROWS,
    }


@dataclass
class MetricAccumulator:
    confusion: np.ndarray = field(default_factory=lambda: np.zeros((2, 2), dtype=np.int64))
    subject_balanced_accuracy: list[float] = field(default_factory=list)
    subjects: int = 0

    def add(self, truth: np.ndarray, prediction: np.ndarray) -> None:
        if not len(truth):
            return
        self.subjects += 1
        encoded = truth.astype(np.int64) * 2 + prediction.astype(np.int64)
        self.confusion += np.bincount(encoded, minlength=4).reshape(2, 2)
        present = set(int(value) for value in np.unique(truth))
        if present == {0, 1}:
            tn, fp, fn, tp = self.confusion_for(truth, prediction)
            self.subject_balanced_accuracy.append(0.5 * (tn / (tn + fp) + tp / (tp + fn)))

    @staticmethod
    def confusion_for(truth: np.ndarray, prediction: np.ndarray) -> tuple[int, int, int, int]:
        tn = int(np.sum((truth == 0) & (prediction == 0)))
        fp = int(np.sum((truth == 0) & (prediction == 1)))
        fn = int(np.sum((truth == 1) & (prediction == 0)))
        tp = int(np.sum((truth == 1) & (prediction == 1)))
        return tn, fp, fn, tp

    def report(self) -> dict:
        tn, fp, fn, tp = self.confusion.ravel().tolist()
        total = tn + fp + fn + tp
        balanced = None
        wake_specificity = None
        sleep_sensitivity = None
        macro_f1 = None
        if tn + fp and tp + fn:
            wake_specificity = tn / (tn + fp)
            sleep_sensitivity = tp / (tp + fn)
            balanced = 0.5 * (wake_specificity + sleep_sensitivity)
        f1_wake_denominator = 2 * tn + fp + fn
        f1_sleep_denominator = 2 * tp + fp + fn
        if f1_wake_denominator and f1_sleep_denominator:
            macro_f1 = 0.5 * (
                (2 * tn / f1_wake_denominator) + (2 * tp / f1_sleep_denominator)
            )
        return {
            "rows": total,
            "subjects": self.subjects,
            "subjects_with_both_classes": len(self.subject_balanced_accuracy),
            "subject_macro_balanced_accuracy": (
                float(np.mean(self.subject_balanced_accuracy)) if self.subject_balanced_accuracy else None
            ),
            "aggregate_balanced_accuracy": balanced,
            "macro_f1": macro_f1,
            "wake_specificity": wake_specificity,
            "sleep_sensitivity": sleep_sensitivity,
            "accuracy": ((tn + tp) / total) if total else None,
            "confusion": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
        }


def evaluate_models(
    models: dict,
    subjects: list[dict],
    subject_dir: Path,
    prediction_output_dir: Path | None = None,
) -> tuple[dict, dict]:
    native = {key: MetricAccumulator() for key in models}
    projected = {key: MetricAccumulator() for key in models}
    metric_handle = None
    if prediction_output_dir is not None:
        prediction_output_dir.mkdir(parents=True, exist_ok=True)
        metric_handle = (prediction_output_dir / "subject_metrics.jsonl").open("w", encoding="utf-8", newline="\n")
    try:
        for subject in subjects:
            arrays = _load_subject(subject_dir / f"{subject['token']}.npz")
            epochs = {seconds: aggregate_epoch(arrays, seconds) for seconds in EPOCH_SECONDS}
            projected_lines: dict[int, np.ndarray] = {}
            projected_predictions: dict[str, tuple[np.ndarray, np.ndarray]] = {}
            for key, model in models.items():
                seconds, candidate_name = key
                candidate = next(item for item in CANDIDATES if item["name"] == candidate_name)
                epoch = epochs[seconds]
                if not len(epoch["y"]):
                    continue
                prediction = model.predict(_candidate_x(epoch["x"], candidate)).astype(np.uint8)
                native[key].add(epoch["y"], prediction)
                rows_per_epoch = seconds // 30
                lines = (epoch["line_start"][:, None] + np.arange(rows_per_epoch, dtype=np.int32)).ravel()
                projected_lines[seconds] = lines
                projected_predictions[f"{seconds}:{candidate_name}"] = (lines, np.repeat(prediction, rows_per_epoch))
            required_epochs = {seconds for seconds, _ in models}
            if set(projected_lines) != required_epochs:
                continue
            common = projected_lines[min(required_epochs)]
            for seconds in sorted(required_epochs - {min(required_epochs)}):
                common = np.intersect1d(common, projected_lines[seconds], assume_unique=True)
            if not len(common):
                continue
            native_positions = np.searchsorted(arrays["line"], common)
            truth = arrays["y_sleep"][native_positions]
            prediction_matrix = []
            prediction_keys = []
            for key in models:
                seconds, candidate_name = key
                rendered_key = f"{seconds}s:{candidate_name}"
                lines, prediction = projected_predictions[f"{seconds}:{candidate_name}"]
                positions = np.searchsorted(lines, common)
                selected_prediction = prediction[positions]
                projected[key].add(truth, selected_prediction)
                prediction_matrix.append(selected_prediction)
                prediction_keys.append(rendered_key)
                if metric_handle is not None:
                    tn, fp, fn, tp = MetricAccumulator.confusion_for(truth, selected_prediction)
                    score = 0.5 * (tn / (tn + fp) + tp / (tp + fn)) if tn + fp and tp + fn else None
                    metric_handle.write(json.dumps({
                        "token": subject["token"],
                        "candidate": rendered_key,
                        "rows": len(truth),
                        "subject_balanced_accuracy": score,
                        "confusion": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
                    }, sort_keys=True) + "\n")
            if prediction_output_dir is not None:
                np.savez_compressed(
                    prediction_output_dir / f"{subject['token']}.npz",
                    line=common.astype(np.int32),
                    t_rel_sec=arrays["t_rel_sec"][native_positions].astype(np.int64),
                    y_sleep=truth.astype(np.uint8),
                    candidate=np.asarray(prediction_keys),
                    prediction=np.vstack(prediction_matrix).astype(np.uint8),
                )
    finally:
        if metric_handle is not None:
            metric_handle.close()
    report = {
        f"{seconds}s:{candidate}": {
            "native_epoch_bins": native[(seconds, candidate)].report(),
            "projected_shared_native_30s_grid": projected[(seconds, candidate)].report(),
            "retrospective_feature_latency_seconds": seconds,
        }
        for seconds, candidate in models
    }
    subject_scores = {
        f"{seconds}s:{candidate}": list(projected[(seconds, candidate)].subject_balanced_accuracy)
        for seconds, candidate in models
    }
    return report, subject_scores


def _fit_candidates(train_subjects: list[dict], subject_dir: Path) -> tuple[dict, dict]:
    models = {}
    sampling = {}
    for seconds in EPOCH_SECONDS:
        x, y, details = collect_training(train_subjects, subject_dir, seconds)
        sampling[str(seconds)] = {**details, "class_counts": dict(Counter(int(value) for value in y))}
        for candidate in CANDIDATES:
            model = _make_model(candidate)
            model.fit(_candidate_x(x, candidate), y)
            models[(seconds, candidate["name"])] = model
    return models, sampling


def _select(validation: dict) -> str:
    ranked = []
    for key, metrics in validation.items():
        score = metrics["projected_shared_native_30s_grid"]["subject_macro_balanced_accuracy"]
        ranked.append((-1.0 if score is None else score, key))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][1]


def _paired_bootstrap(selected: list[float], baseline: list[float], draws: int = 2_000) -> dict:
    if len(selected) != len(baseline):
        raise ValueError("paired subject score lengths differ")
    if not selected:
        return {"subjects": 0, "draws": draws, "mean_difference": None, "ci95": None, "probability_gt_zero": None}
    difference = np.asarray(selected, dtype=np.float64) - np.asarray(baseline, dtype=np.float64)
    rng = np.random.default_rng(42)
    means = np.empty(draws, dtype=np.float64)
    for index in range(draws):
        means[index] = difference[rng.integers(0, len(difference), size=len(difference))].mean()
    return {
        "subjects": len(difference),
        "draws": draws,
        "mean_difference": float(difference.mean()),
        "ci95": [float(value) for value in np.percentile(means, [2.5, 97.5])],
        "probability_gt_zero": float(np.mean(means > 0)),
        "random_seed": 42,
    }


def _validation_learning_curve(train_subjects: list[dict], validation_subjects: list[dict], subject_dir: Path) -> list[dict]:
    candidate = next(item for item in CANDIDATES if item["name"] == "activity_light_hgb")
    sizes = []
    for requested in (100, 300, 600, len(train_subjects)):
        size = min(requested, len(train_subjects))
        if size and size not in sizes:
            sizes.append(size)
    curve = []
    for size in sizes:
        x, y, sampling = collect_training(train_subjects[:size], subject_dir, 60)
        model = _make_model(candidate)
        model.fit(_candidate_x(x, candidate), y)
        key = (60, candidate["name"])
        metrics, _ = evaluate_models({key: model}, validation_subjects, subject_dir)
        curve.append({
            "requested_train_subjects": size,
            "sampling": sampling,
            "validation_native_60s": metrics["60s:activity_light_hgb"]["native_epoch_bins"],
        })
    return curve


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True) + "\n", encoding="utf-8")


def _source_code_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _finish_experiment(
    stage: Path,
    output: Path,
    manifest: dict,
    resume_provenance: dict | None = None,
) -> dict:
    subject_dir = stage / "subjects"
    subjects = manifest["subjects"]
    by_split = {
        name: [subject for subject in subjects if subject["split"] == name]
        for name in ("train", "validation", "test")
    }
    models, sampling = _fit_candidates(by_split["train"], subject_dir)
    print(json.dumps({"phase": "evaluate", "split": "validation"}), flush=True)
    protocol = {
        "schema_version": 1,
        "frozen_at_utc": _utc_now(),
        "label": "actigraphy-derived weak y_sleep=1-wake",
        "epochs_seconds": list(EPOCH_SECONDS),
        "candidates": list(CANDIDATES),
        "selection_metric": "validation projected shared-native-grid subject-macro balanced accuracy",
        "test_policy": "all frozen candidates reported descriptively; test does not alter selection",
        "common_support_seconds": 600,
        "training_sampling": sampling,
        "native_thread_bound": 2,
        "source_code_sha256": _source_code_sha256(),
        "resume": resume_provenance,
    }
    _write_json(stage / "protocol.json", protocol)
    validation, _ = evaluate_models(
        models,
        by_split["validation"],
        subject_dir,
        stage / "evaluation" / "validation",
    )
    selected = _select(validation)
    selected_record = {
        "selected_at_utc": _utc_now(),
        "selected_before_test": selected,
        "selection_metric": protocol["selection_metric"],
        "validation_metrics": validation[selected],
    }
    _write_json(stage / "selected.json", selected_record)
    print(json.dumps({"phase": "validation_learning_curve", "sizes": [100, 300, 600, "all"]}), flush=True)
    learning_curve = _validation_learning_curve(by_split["train"], by_split["validation"], subject_dir)
    print(json.dumps({"phase": "evaluate", "split": "test", "selected_before_test": selected}), flush=True)
    test, test_subject_scores = evaluate_models(
        models,
        by_split["test"],
        subject_dir,
        stage / "evaluation" / "test",
    )

    selected_epoch_text, selected_name = selected.split(":", 1)
    selected_epoch = int(selected_epoch_text.removesuffix("s"))
    dummy_key = f"{selected_epoch}s:dummy_prior"
    bootstrap = _paired_bootstrap(test_subject_scores[selected], test_subject_scores[dummy_key])
    selected_candidate = next(item for item in CANDIDATES if item["name"] == selected_name)
    evaluated_model = models[(selected_epoch, selected_name)]
    artifact = {
        "model": evaluated_model,
        "epoch_seconds": selected_epoch,
        "candidate": selected_name,
        "feature_names": list(selected_candidate["features"]),
        "label": "actigraphy-derived weak y_sleep=1-wake",
        "fit_splits": ["train"],
        "evaluation_splits": ["validation", "test"],
        "research_only": True,
    }
    joblib.dump(artifact, stage / "selected_model.joblib", compress=3)
    artifact_sha256 = hashlib.sha256((stage / "selected_model.joblib").read_bytes()).hexdigest()

    report = {
        "schema_version": 1,
        "completed_at_utc": _utc_now(),
        "purpose": "research-only MESA actigraphy weak-label epoch/model comparison",
        "protocol_frozen_before_test": True,
        "selection_metric": protocol["selection_metric"],
        "candidate_protocol": {
            "epochs_seconds": list(EPOCH_SECONDS),
            "models": list(CANDIDATES),
            "hgb_max_iter": 60,
            "training_sampling": sampling,
            "native_thread_bound": 2,
        },
        "common_support": {
            "source": "native 30-second rows inside fully observed ten-minute blocks (LCM of 30/60/120/300 seconds)",
            "projection": "each retrospective epoch-bin prediction repeated over its contributing native rows; intersection across all epochs",
            "warning": "coarser averaged sensor input is end-of-bin retrospective classification, not forecasting",
        },
        "validation": validation,
        "validation_only_training_size_curve_fixed_60s_hgb": learning_curve,
        "selected_before_test": selected,
        "test_all_frozen_candidates_descriptive": test,
        "test_selected_vs_same_epoch_dummy_paired_subject_bootstrap": bootstrap,
        "selected_artifact": {
            "file": "selected_model.joblib",
            "sha256": artifact_sha256,
            "exact_evaluated_model": True,
            "fit_splits": ["train"],
            "sampling": sampling[str(selected_epoch)],
            "research_only": True,
        },
    }
    _write_json(stage / "experiment_report.json", report)
    os.replace(stage, output)
    return report


def run_experiment(raw_root: Path, output: Path) -> dict:
    raw_root = raw_root.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    if raw_root == output or raw_root in output.parents:
        raise ValueError("output must be outside raw root")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.parent / f".{output.name}.staging-{os.getpid()}"
    if stage.exists():
        raise FileExistsError(f"staging directory already exists: {stage}")
    subject_dir = stage / "subjects"
    subject_dir.mkdir(parents=True)

    started = _utc_now()
    candidates = []
    skipped_nonregular = 0
    for path in sorted(raw_root.rglob("*.csv"), key=lambda item: item.relative_to(raw_root).as_posix()):
        if path.is_symlink() or not path.is_file() or raw_root not in path.resolve(strict=True).parents:
            skipped_nonregular += 1
            continue
        participant = participant_from_name(path)
        if participant is not None:
            candidates.append((path, participant, _signature(path)))
    discovery_count = len(candidates)
    discovery_bytes = sum(signature[0] for _, _, signature in candidates if signature)
    subjects = []
    excluded = Counter()
    aggregate_qc = Counter()
    seen_participants = set()
    try:
        for index, (path, participant, before) in enumerate(candidates, 1):
            if participant in seen_participants:
                excluded["duplicate_participant_file"] += 1
                continue
            try:
                arrays, qc, digest = extract_common_rows(path, participant)
            except (OSError, UnicodeError, csv.Error, ValueError):
                excluded["read_or_schema_error"] += 1
                continue
            after = _signature(path)
            if before is None or after != before:
                excluded["changed_during_read"] += 1
                continue
            if not len(arrays["line"]):
                excluded["no_complete_eligible_ten_minute_blocks"] += 1
                continue
            token = subject_token(participant)
            temporary = subject_dir / f".{token}.tmp.npz"
            np.savez_compressed(temporary, **arrays)
            os.replace(temporary, subject_dir / f"{token}.npz")
            split = subject_split(participant)
            record = {
                "token": token,
                "split": split,
                "source_sha256": digest,
                "source_size": before[0],
                "source_mtime_ns": before[1],
                "source_rows": qc.get("source_rows", 0),
                "eligible_native_rows": qc.get("eligible_native_rows", 0),
                "eligible_ten_minute_blocks": qc.get("eligible_ten_minute_blocks", 0),
            }
            subjects.append(record)
            seen_participants.add(participant)
            aggregate_qc.update(qc)
            if index % 100 == 0 or index == discovery_count:
                print(json.dumps({"phase": "convert", "processed": index, "discovered": discovery_count}), flush=True)

        ending_candidates = [
            path for path in raw_root.rglob("*.csv")
            if not path.is_symlink() and path.is_file() and participant_from_name(path) is not None
        ]
        subjects.sort(key=lambda item: item["token"])
        split_counts = Counter(subject["split"] for subject in subjects)
        manifest = {
            "schema_version": 1,
            "created_at_utc": _utc_now(),
            "raw_snapshot": {
                "discovered_at_utc": started,
                "candidate_files": discovery_count,
                "candidate_bytes": discovery_bytes,
                "ending_candidate_files": len(ending_candidates),
                "files_added_during_run": max(0, len(ending_candidates) - discovery_count),
                "skipped_nonregular_or_outside": skipped_nonregular,
                "excluded": dict(sorted(excluded.items())),
            },
            "label": {
                "name": "y_sleep",
                "formula": "1 - wake",
                "status": "actigraphy-derived weak label, not PSG ground truth",
                "wake_and_interval_excluded_from_features": True,
                "date_shifted_not_interpreted_as_calendar_date": True,
            },
            "features": list(FEATURE_NAMES),
            "eligibility": "offwrist == 0; binary wake; all five sensors finite; complete 20-row/10-minute blocks",
            "split": {
                "method": "stable SHA-256 participant hash buckets",
                "seed_namespace": SPLIT_SEED,
                "buckets": {"train": "0-69", "validation": "70-84", "test": "85-99"},
                "counts": dict(sorted(split_counts.items())),
                "participant_overlap": 0,
            },
            "aggregate_qc": dict(sorted(aggregate_qc.items())),
            "subjects": subjects,
        }
        _write_json(stage / "manifest.json", manifest)

        return _finish_experiment(stage, output, manifest)
    except BaseException:
        print(json.dumps({"completed": False, "staging_preserved": str(stage)}), flush=True)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resume_experiment(stage: Path, output: Path, previous_code_sha256: str) -> dict:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = stage.resolve(strict=True)
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    expected_stage_prefix = f".{output.name}.staging-"
    if stage.parent != output.parent or not stage.name.startswith(expected_stage_prefix):
        raise ValueError("resume stage must be the exact output staging directory under the output parent")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", previous_code_sha256):
        raise ValueError("previous code SHA-256 must be 64 hexadecimal characters")
    for forbidden in (
        stage / "protocol.json",
        stage / "selected.json",
        stage / "evaluation" / "test",
        stage / "experiment_report.json",
    ):
        if forbidden.exists():
            raise ValueError("resume is allowed only before protocol freeze, selection, and test evaluation")

    manifest_path = stage / "manifest.json"
    manifest_sha256 = _sha256_file(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    subjects = manifest.get("subjects")
    if not isinstance(subjects, list) or not subjects:
        raise ValueError("resume manifest has no subjects")
    tokens = [subject.get("token") for subject in subjects]
    if len(tokens) != len(set(tokens)) or any(
        not isinstance(token, str) or not re.fullmatch(r"mesa_[0-9a-f]{20}", token)
        for token in tokens
    ):
        raise ValueError("resume manifest tokens are invalid or duplicated")
    if any(subject.get("split") not in {"train", "validation", "test"} for subject in subjects):
        raise ValueError("resume manifest contains an invalid split")
    actual_split_counts = Counter(subject["split"] for subject in subjects)
    if dict(sorted(actual_split_counts.items())) != manifest.get("split", {}).get("counts"):
        raise ValueError("resume manifest split counts do not match subjects")

    subject_dir = stage / "subjects"
    expected_names = {f"{token}.npz" for token in tokens}
    actual_files = [path for path in subject_dir.iterdir() if path.is_file() and not path.is_symlink()]
    if {path.name for path in actual_files} != expected_names or len(actual_files) != len(expected_names):
        raise ValueError("resume subject archive set does not exactly match the manifest")

    archive_hashes = {}
    aggregate_digest = hashlib.sha256()
    for index, subject in enumerate(subjects, 1):
        token = subject["token"]
        path = subject_dir / f"{token}.npz"
        before = _signature(path)
        archive_sha256 = _sha256_file(path)
        arrays = _load_subject(path)
        after = _signature(path)
        if before is None or before != after:
            raise ValueError("resume subject archive changed during validation")
        line = arrays["line"]
        relative = arrays["t_rel_sec"]
        x = arrays["x"]
        y = arrays["y_sleep"]
        length = len(line)
        if (
            line.ndim != 1
            or relative.ndim != 1
            or x.shape != (length, len(FEATURE_NAMES))
            or y.shape != (length,)
            or x.dtype != np.float32
            or not length
        ):
            raise ValueError("resume subject archive has invalid shapes or dtypes")
        if (
            np.any(np.diff(line.astype(np.int64)) <= 0)
            or np.any(np.diff(relative.astype(np.int64)) != np.diff(line.astype(np.int64)) * 30)
            or not np.all(np.isfinite(x))
            or np.any(x < 0)
            or not np.all(np.isin(y, (0, 1)))
        ):
            raise ValueError("resume subject archive failed line, time, sensor, or label validation")
        if length != subject.get("eligible_native_rows"):
            raise ValueError("resume subject archive row count differs from the manifest")
        archive_hashes[token] = archive_sha256
        aggregate_digest.update(token.encode("ascii") + b"\0" + archive_sha256.encode("ascii") + b"\n")
        if index % 250 == 0 or index == len(subjects):
            print(json.dumps({"phase": "resume_validate", "processed": index, "subjects": len(subjects)}), flush=True)

    provenance = {
        "validated_at_utc": _utc_now(),
        "stage_name": stage.name,
        "manifest_sha256": manifest_sha256,
        "subject_archives": len(archive_hashes),
        "subject_archive_hashes_sha256": aggregate_digest.hexdigest(),
        "previous_source_code_sha256": previous_code_sha256.lower(),
        "resumed_source_code_sha256": _source_code_sha256(),
        "test_artifacts_present_before_resume": False,
    }
    validation_record = {**provenance, "archive_sha256_by_token": archive_hashes}
    _write_json(stage / "resume_validation.json", validation_record)
    provenance["resume_validation_file"] = "resume_validation.json"
    provenance["resume_validation_file_sha256"] = _sha256_file(stage / "resume_validation.json")
    try:
        return _finish_experiment(stage, output, manifest, provenance)
    except BaseException:
        print(json.dumps({"completed": False, "staging_preserved": str(stage)}), flush=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--root", type=Path, help="MESA root containing actigraphy CSV files")
    source.add_argument("--resume-stage", type=Path, help="Validated pre-test staging directory to resume")
    parser.add_argument("--output", type=Path, required=True, help="New private research output directory")
    parser.add_argument("--previous-code-sha256", help="Required with --resume-stage for provenance")
    args = parser.parse_args()
    if args.resume_stage is not None:
        if not args.previous_code_sha256:
            parser.error("--previous-code-sha256 is required with --resume-stage")
        report = resume_experiment(args.resume_stage, args.output, args.previous_code_sha256)
    else:
        if args.previous_code_sha256:
            parser.error("--previous-code-sha256 is only valid with --resume-stage")
        report = run_experiment(args.root, args.output)
    print(json.dumps({"completed": True, "selected": report["selected_before_test"]}), flush=True)


if __name__ == "__main__":
    main()
