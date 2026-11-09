from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.isotonic import IsotonicRegression
from threadpoolctl import threadpool_limits

HERE = Path(__file__).resolve().parent
DEFAULT_WORKSPACE = HERE.parents[2]
DEFAULT_WEB = DEFAULT_WORKSPACE / "기타/SleepWell-integrated/web-app"
if str(DEFAULT_WEB) not in sys.path:
    sys.path.insert(0, str(DEFAULT_WEB))
from night_core import ZONE, physical_prediction, stationary_thresholds

SEED = 20261028
TAU_WAKE_H = 18.2
TAU_SLEEP_H = 4.2
STEP_H = 30 / 3600
BOOTSTRAPS = 2000
FEATURE_NAMES = ("activity", "whitelight", "redlight", "greenlight", "bluelight")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected object: {path}")
    return value


def write_json_new(path: Path, value) -> None:
    with Path(path).open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def write_csv_new(path: Path, frame: pd.DataFrame) -> None:
    with Path(path).open("x", encoding="utf-8", newline="") as handle:
        frame.to_csv(handle, index=False)


def subject_token(participant: int) -> str:
    value = hashlib.sha256(f"mesa_subject_v1|{int(participant)}".encode()).hexdigest()
    return f"mesa_{value[:20]}"


def token_id_map(maximum: int = 9999) -> dict[str, int]:
    return {subject_token(value): value for value in range(1, maximum + 1)}


def aggregate_five_minute(arrays: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    lines = np.asarray(arrays["line"], dtype=np.int64)
    x = np.asarray(arrays["x"], dtype=float)
    y = np.asarray(arrays["y_sleep"], dtype=float)
    rows = 10
    bins = (lines - 1) // rows
    changes = np.flatnonzero(np.diff(bins)) + 1
    starts = np.r_[0, changes]
    ends = np.r_[changes, len(lines)]
    counts = ends - starts
    expected = bins[starts] * rows + 1
    complete = ((counts == rows) & (lines[starts] == expected)
                & (lines[ends - 1] == expected + rows - 1))
    return {
        "line_start": expected[complete].astype(np.int64),
        "x": (np.add.reduceat(x, starts, axis=0)[complete] / rows).astype(np.float32),
        "sleep_fraction": (np.add.reduceat(y, starts)[complete] / rows).astype(float),
        "majority": (np.add.reduceat(y, starts)[complete] * 2 >= rows).astype(np.uint8),
    }


def project_bins(lines: np.ndarray, line_starts: np.ndarray, values: np.ndarray) -> np.ndarray:
    rendered_lines = (line_starts[:, None] + np.arange(10, dtype=np.int64)).ravel()
    rendered_values = np.repeat(np.asarray(values), 10)
    positions = np.searchsorted(rendered_lines, lines)
    if np.any(positions >= len(rendered_lines)) or not np.array_equal(rendered_lines[positions], lines):
        raise ValueError("Every eligible 30-second row must have a complete five-minute prediction")
    return rendered_values[positions]


def contiguous_segments(lines: np.ndarray) -> list[np.ndarray]:
    lines = np.asarray(lines)
    if not len(lines):
        return []
    return [part for part in np.split(np.arange(len(lines)), np.flatnonzero(np.diff(lines) != 1) + 1) if len(part)]


def update_s(s: float, p_sleep: float, hours: float = STEP_H) -> float:
    if not (0 <= p_sleep <= 1) or not math.isfinite(p_sleep) or hours < 0:
        raise ValueError("Invalid Process S update")
    sleep_next = s * math.exp(-hours / TAU_SLEEP_H)
    wake_next = 1 - (1 - s) * math.exp(-hours / TAU_WAKE_H)
    return p_sleep * sleep_next + (1 - p_sleep) * wake_next


def trajectory(probability: np.ndarray, initial_s: float) -> np.ndarray:
    result = np.empty(len(probability), dtype=float)
    state = float(initial_s)
    for index, value in enumerate(np.asarray(probability, dtype=float)):
        state = update_s(state, float(value))
        result[index] = state
    return result


def advance_awake(s: float, hours: float) -> float:
    if hours < 0:
        raise ValueError("Cannot advance backwards")
    return 1 - (1 - s) * math.exp(-hours / TAU_WAKE_H)


def next_twenty_strictly_after(value: datetime) -> datetime:
    candidate = value.replace(hour=20, minute=0, second=0, microsecond=0)
    return candidate if candidate > value else candidate + timedelta(days=1)


def ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    total = len(y)
    if not total:
        return float("nan")
    edges = np.linspace(0, 1, bins + 1)
    score = 0.0
    for index in range(bins):
        mask = ((p >= edges[index]) & (p < edges[index + 1])) if index < bins - 1 else ((p >= edges[index]) & (p <= 1))
        if mask.any():
            score += mask.mean() * abs(float(p[mask].mean() - y[mask].mean()))
    return score


def participant_macro_probability(rows: list[dict], probability_key: str) -> dict:
    values = []
    for row in rows:
        y = row["truth"]
        p = row[probability_key]
        values.append({"token": row["token"], "brier": float(np.mean((p - y) ** 2)), "ece": ece(y, p)})
    return {
        "n_people": len(values),
        "brier": float(np.mean([item["brier"] for item in values])),
        "ece": float(np.mean([item["ece"] for item in values])),
        "per_person": values,
    }


def bootstrap_mean(values: Iterable[float], seed: int = SEED) -> list[float]:
    array = np.asarray(list(values), dtype=float)
    if not len(array):
        return [float("nan"), float("nan")]
    rng = np.random.default_rng(seed)
    samples = array[rng.integers(0, len(array), size=(BOOTSTRAPS, len(array)))].mean(axis=1)
    return [float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975))]


def summarize(values: Iterable[float]) -> dict:
    array = np.asarray(list(values), dtype=float)
    return {
        "mean": float(array.mean()),
        "median": float(np.median(array)),
        "ci95": bootstrap_mean(array),
        "n_people": int(len(array)),
    }


@lru_cache(maxsize=None)
def raw_origin(path: Path) -> datetime:
    first = pd.read_csv(path, usecols=["line", "date_shifted", "linetime"], nrows=1).iloc[0]
    line = int(first["line"])
    stamp = datetime.fromisoformat(f"{first['date_shifted']}T{first['linetime']}")
    return stamp - timedelta(seconds=(line - 1) * 30)


def raw_path(raw_dir: Path, participant: int) -> Path:
    candidates = {raw_dir / f"mesa-sleep-{participant:04d}.csv", raw_dir / f"mesa-sleep-{participant}.csv"}
    found = [path for path in candidates if path.is_file()]
    if len(found) != 1:
        raise FileNotFoundError(f"Expected one ACT file for participant {participant}")
    return found[0]


def load_subject_rows(subjects: list[dict], subject_dir: Path, model, calibrator=None) -> list[dict]:
    output = []
    for subject in subjects:
        path = subject_dir / f"{subject['token']}.npz"
        with np.load(path, allow_pickle=False) as saved:
            arrays = {key: saved[key] for key in saved.files}
        epoch = aggregate_five_minute(arrays)
        raw_p_bin = model.predict_proba(epoch["x"])[:, 1].astype(float)
        binary_bin = model.predict(epoch["x"]).astype(float)
        calibrated_bin = calibrator.predict(raw_p_bin) if calibrator is not None else raw_p_bin
        lines = arrays["line"].astype(np.int64)
        output.append({
            "token": subject["token"], "split": subject["split"], "lines": lines,
            "truth": arrays["y_sleep"].astype(float),
            "actigraphy": arrays["y_actigraphy"].astype(float),
            "raw_probability": project_bins(lines, epoch["line_start"], raw_p_bin),
            "binary": project_bins(lines, epoch["line_start"], binary_bin),
            "calibrated_probability": project_bins(lines, epoch["line_start"], calibrated_bin),
            "bin_raw_probability": raw_p_bin,
            "bin_sleep_fraction": epoch["sleep_fraction"],
        })
    return output


def fit_calibration(validation_rows: list[dict]) -> tuple[IsotonicRegression, dict]:
    probabilities = np.concatenate([row["bin_raw_probability"] for row in validation_rows])
    fractions = np.concatenate([row["bin_sleep_fraction"] for row in validation_rows])
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)
    calibrator.fit(probabilities, fractions)
    return calibrator, {
        "x_thresholds": [float(value) for value in calibrator.X_thresholds_],
        "y_thresholds": [float(value) for value in calibrator.y_thresholds_],
        "fit_split": "validation",
        "fit_bins": int(len(probabilities)),
        "fit_people": int(len(validation_rows)),
    }


def fixed_physical_prediction(s: float, cutoff: datetime) -> tuple[float, float]:
    phase, high, low = stationary_thresholds(180.0, 660.0)
    return physical_prediction(s, cutoff.replace(tzinfo=ZONE), phase, high, low, (180.0, 660.0))[:2]


def evaluate_subject(row: dict, participant: int, raw_dir: Path, support: str, initial_s: float) -> dict | None:
    segments = contiguous_segments(row["lines"])
    if support == "primary":
        if len(segments) != 1:
            return None
        selected = segments[0]
    elif support == "secondary":
        selected = max(segments, key=len)
    else:
        raise ValueError(support)
    if len(selected) < 120:
        return None
    truth = row["truth"][selected]
    if len(np.unique(truth)) < 2:
        return None
    lines = row["lines"][selected]
    first_sleep = int(np.flatnonzero(truth == 1)[0])
    last_sleep = int(np.flatnonzero(truth == 1)[-1])
    diary = np.zeros(len(truth), dtype=float)
    diary[first_sleep:last_sleep + 1] = 1
    methods = {
        "classifier_binary": row["binary"][selected],
        "classifier_raw_probability": row["raw_probability"][selected],
        "classifier_calibrated_probability": row["calibrated_probability"][selected],
        "actigraphy_binary": row["actigraphy"][selected],
        "diary_like_psg_first_to_last_sleep": diary,
    }
    reference = trajectory(truth, initial_s)
    source = raw_path(raw_dir, participant)
    origin = raw_origin(source)
    segment_start = origin + timedelta(seconds=(int(lines[0]) - 1) * 30)
    segment_end = origin + timedelta(seconds=int(lines[-1]) * 30)
    cutoff = next_twenty_strictly_after(segment_end)
    gap_hours = (cutoff - segment_end).total_seconds() / 3600
    if not (0 < gap_hours <= 24):
        raise ValueError("Unexpected cutoff gap")
    reference_20 = advance_awake(float(reference[-1]), gap_hours)
    oracle_bed, oracle_wake = fixed_physical_prediction(reference_20, cutoff)
    wake_inside = float(np.sum((truth == 0) & (np.arange(len(truth)) >= first_sleep) & (np.arange(len(truth)) <= last_sleep)) * 0.5)
    result = {
        "token": row["token"], "support": support, "initial_s": initial_s,
        "rows_30s": int(len(truth)), "segment_start": segment_start.isoformat(),
        "segment_end": segment_end.isoformat(), "cutoff": cutoff.isoformat(),
        "gap_to_cutoff_h": gap_hours, "psg_sleep_fraction": float(truth.mean()),
        "psg_wake_inside_diary_interval_min": wake_inside,
        "psg_reference_s_end": float(reference[-1]), "psg_reference_s_20": reference_20,
        "oracle_physical_onset_offset_min": oracle_bed,
        "oracle_physical_wake_offset_min": oracle_wake,
    }
    for name, probability in methods.items():
        estimated = trajectory(probability, initial_s)
        estimated_20 = advance_awake(float(estimated[-1]), gap_hours)
        bed, wake = fixed_physical_prediction(estimated_20, cutoff)
        result.update({
            f"{name}_trajectory_mae": float(np.mean(np.abs(estimated - reference))),
            f"{name}_s_end_abs_error": abs(float(estimated[-1] - reference[-1])),
            f"{name}_s_20_abs_error": abs(float(estimated_20 - reference_20)),
            f"{name}_onset_abs_difference_min": abs(float(bed - oracle_bed)),
            f"{name}_wake_abs_difference_min": abs(float(wake - oracle_wake)),
        })
    return result


def aggregate_process_metrics(frame: pd.DataFrame, probability_selection: str) -> dict:
    methods = [
        "classifier_binary", "classifier_raw_probability", "classifier_calibrated_probability",
        "actigraphy_binary", "diary_like_psg_first_to_last_sleep",
    ]
    metrics = {}
    for method in methods:
        metrics[method] = {}
        for suffix in ("trajectory_mae", "s_end_abs_error", "s_20_abs_error",
                       "onset_abs_difference_min", "wake_abs_difference_min"):
            metrics[method][suffix] = summarize(frame[f"{method}_{suffix}"])
    selected = "classifier_calibrated_probability" if probability_selection == "validation_isotonic_occupancy_probability" else "classifier_raw_probability"
    for metric in ("trajectory_mae", "s_end_abs_error", "s_20_abs_error"):
        gain = frame[f"diary_like_psg_first_to_last_sleep_{metric}"] - frame[f"{selected}_{metric}"]
        metrics[f"selected_probability_gain_vs_diary_{metric}"] = summarize(gain)
    metrics["fragmentation"] = {
        "wake_inside_diary_interval_min": summarize(frame["psg_wake_inside_diary_interval_min"]),
        "correlation_wake_inside_vs_diary_trajectory_error": (
            float(frame["psg_wake_inside_diary_interval_min"].corr(
                frame["diary_like_psg_first_to_last_sleep_trajectory_mae"]))
            if len(frame) > 1 else None
        ),
    }
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    experiment = HERE
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    psg_root = workspace / "데이터모델/연구용데이터/mesa_psg_run_20261020_v2"
    raw_dir = workspace / "데이터셋/MESA/actigraphy"
    protocol = load_json(experiment / "protocol.json")
    expected_hashes = {
        "selected_model.joblib": protocol["inputs"]["classifier_sha256"],
        "manifest.json": protocol["inputs"]["manifest_sha256"],
    }
    for name, expected in expected_hashes.items():
        actual = sha256(psg_root / name)
        if actual != expected:
            raise ValueError(f"Input hash mismatch: {name}")
    artifact = joblib.load(psg_root / "selected_model.joblib")
    if (artifact.get("epoch_seconds") != 300 or artifact.get("candidate") != "activity_light_hgb"
            or artifact.get("fit_splits") != ["train"] or artifact.get("research_only") is not True
            or tuple(artifact.get("feature_names", ())) != FEATURE_NAMES):
        raise ValueError("Classifier artifact contract mismatch")
    manifest = load_json(psg_root / "manifest.json")
    subjects = manifest["subjects"]
    validation = [row for row in subjects if row["split"] == "validation"]
    test = [row for row in subjects if row["split"] == "test"]
    if (len(validation), len(test)) != (144, 151):
        raise ValueError("Unexpected split counts")
    model = artifact["model"]
    with threadpool_limits(limits=2):
        validation_raw = load_subject_rows(validation, psg_root / "subjects", model)
        calibrator, calibration = fit_calibration(validation_raw)
        validation_rows = load_subject_rows(validation, psg_root / "subjects", model, calibrator)
        raw_validation = participant_macro_probability(validation_rows, "raw_probability")
        calibrated_validation = participant_macro_probability(validation_rows, "calibrated_probability")
        if calibrated_validation["brier"] < raw_validation["brier"] - 1e-15:
            selection = "validation_isotonic_occupancy_probability"
        else:
            selection = "raw_hgb_probability"
    selection_lock = {
        "selected_before_test_process_s_scoring": selection,
        "metric": "participant_macro_projected_30s_brier",
        "raw_validation": {key: value for key, value in raw_validation.items() if key != "per_person"},
        "isotonic_validation": {key: value for key, value in calibrated_validation.items() if key != "per_person"},
        "calibration": calibration,
        "test_consulted": False,
    }
    write_json_new(output / "selection_lock.json", selection_lock)
    with threadpool_limits(limits=2):
        test_rows = load_subject_rows(test, psg_root / "subjects", model, calibrator)
    test_probability = {
        "raw": participant_macro_probability(test_rows, "raw_probability"),
        "isotonic": participant_macro_probability(test_rows, "calibrated_probability"),
    }
    ids = token_id_map()
    if any(row["token"] not in ids for row in test):
        raise ValueError("Could not resolve private raw ACT path tokens")
    result_rows = []
    for initial_s in protocol["process_s"]["initial_s_sensitivity"]:
        for support in ("primary", "secondary"):
            for row in test_rows:
                evaluated = evaluate_subject(row, ids[row["token"]], raw_dir, support, float(initial_s))
                if evaluated is not None:
                    result_rows.append(evaluated)
    results = pd.DataFrame(result_rows).sort_values(["initial_s", "support", "token"]).reset_index(drop=True)
    if results.empty:
        raise ValueError("No evaluable Test participants")
    write_csv_new(output / "participant_metrics.csv", results)
    primary = results[(results["support"] == "primary") & (results["initial_s"] == 0.5)].copy()
    secondary = results[(results["support"] == "secondary") & (results["initial_s"] == 0.5)].copy()
    sensitivity = {}
    for initial_s in protocol["process_s"]["initial_s_sensitivity"]:
        frame = results[(results["support"] == "secondary") & (results["initial_s"] == float(initial_s))]
        sensitivity[str(initial_s)] = aggregate_process_metrics(frame, selection)
    metrics = {
        "schema_version": 1,
        "experiment_id": protocol["experiment_id"],
        "probability_selection": selection_lock,
        "test_probability": {
            name: {key: value for key, value in report.items() if key != "per_person"}
            for name, report in test_probability.items()
        },
        "primary_single_contiguous": aggregate_process_metrics(primary, selection),
        "secondary_longest_contiguous": aggregate_process_metrics(secondary, selection),
        "initial_s_sensitivity_secondary": sensitivity,
        "counts": {
            "validation_people": len(validation), "test_people": len(test),
            "primary_people": int(primary["token"].nunique()),
            "secondary_people": int(secondary["token"].nunique()),
        },
        "interpretation": {
            "s_reference": "PSG-driven Process S under fixed tau and initial conditions; not measured physiology",
            "propagation": "difference from oracle-S physical output; not actual next-night MAE",
            "daytime": "awake propagation from PSG end to 20:00 is assumed and daytime naps are not observed",
            "service_promotion": False,
        },
    }
    write_json_new(output / "metrics.json", metrics)
    source_files = [experiment / "protocol.json", Path(__file__), psg_root / "selected_model.joblib", psg_root / "manifest.json"]
    write_json_new(output / "source_hashes.json", {str(path.relative_to(workspace)): sha256(path) for path in source_files})
    write_json_new(output / "environment.json", {
        "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
        "sklearn": sklearn.__version__, "joblib": joblib.__version__,
    })
    print(json.dumps({
        "selection": selection, "primary_people": metrics["counts"]["primary_people"],
        "secondary_people": metrics["counts"]["secondary_people"],
        "primary_selected_s20_mae": metrics["primary_single_contiguous"][
            "classifier_calibrated_probability" if selection.startswith("validation_isotonic") else "classifier_raw_probability"
        ]["s_20_abs_error"]["mean"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
