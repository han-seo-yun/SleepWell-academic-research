"""Causal, aggregate evaluation of the frozen ``gradual_v1`` inference policy.

The input is a private normalized CSV.  Aggregate exports never contain participant
identifiers.  Frozen robust-v2 model parameters are loaded through the verified
vendor adapter; this module does not fit or select a model.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from researchbench.academic_backend import AcademicRunCancelled, get_provenance, vendor_module


SCHEMA = "sleepwell.gradual_evaluation.v1"
POLICY = "gradual_v1"
COMPARATOR = "legacy14"
STATUS = "retrospective_reused_holdout"
COHORTS = ("cmi", "mesa")
ARMS = ("data", "hybrid", "physical", "recent_median")
ENDPOINTS = ("onset", "wake", "joint", "interval")
WEIGHTINGS = ("night", "participant")
REQUIRED_COLUMNS = (
    "cohort", "participant_id", "split", "row_id", "cutoff", "bed", "wake",
    "observed_at", "bed_offset", "wake_offset",
)
REFERENCE_COLUMNS = tuple(
    f"legacy_{arm}_{endpoint}"
    for arm in ("data", "hybrid", "physical")
    for endpoint in ("bed", "wake")
)
PARITY_TOLERANCE_MIN = 1e-7


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _emit(progress: Callable[[dict[str, Any]], None] | None, stage: str, value: float) -> None:
    if progress:
        progress({"stage": stage, "progress": max(0.0, min(1.0, value))})


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled and cancelled():
        raise AcademicRunCancelled("Gradual-policy evaluation cancelled.")


def _float(value: str, name: str, *, optional: bool = False) -> float | None:
    text = (value or "").strip()
    if optional and not text:
        return None
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"{name} is not finite")
    return number


def _read_input(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    raw_split_by_person: dict[tuple[str, str], set[str]] = defaultdict(set)
    raw_row_ids: list[tuple[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames or []
        missing = [name for name in REQUIRED_COLUMNS if name not in columns]
        if missing:
            raise ValueError("Input CSV missing required columns: " + ", ".join(missing))
        for line_number, raw in enumerate(reader, 2):
            raw_cohort = (raw.get("cohort") or "").strip().lower()
            raw_split = (raw.get("split") or "").strip().lower()
            raw_participant = (raw.get("participant_id") or "").strip()
            raw_row_id = (raw.get("row_id") or "").strip()
            if raw_cohort in COHORTS and raw_participant and raw_split in {"train", "validation", "test"}:
                raw_split_by_person[(raw_cohort, raw_participant)].add(raw_split)
            if raw_cohort in COHORTS and raw_row_id:
                raw_row_ids.append((raw_cohort, raw_row_id))
            try:
                cohort = raw["cohort"].strip().lower()
                split = raw["split"].strip().lower()
                participant = raw["participant_id"].strip()
                if cohort not in COHORTS:
                    raise ValueError("unsupported cohort")
                if split not in {"train", "validation", "test"}:
                    raise ValueError("unsupported split")
                if not participant:
                    raise ValueError("participant_id is empty")
                if not raw_row_id:
                    raise ValueError("row_id is empty")
                cutoff = datetime.fromisoformat(raw["cutoff"])
                bed = datetime.fromisoformat(raw["bed"])
                wake = datetime.fromisoformat(raw["wake"])
                observed = datetime.fromisoformat(raw["observed_at"])
                if cutoff.tzinfo is None or bed.tzinfo is None or wake.tzinfo is None or observed.tzinfo is None:
                    raise ValueError("timestamps must include an offset")
                if (cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond) != (20, 0, 0, 0):
                    raise ValueError("cutoff must be exactly local 20:00")
                if not (cutoff.utcoffset() == bed.utcoffset() == wake.utcoffset() == observed.utcoffset()):
                    raise ValueError("timestamp UTC offsets must match within a row")
                if not cutoff <= bed < wake <= observed:
                    raise ValueError("timestamps must satisfy cutoff <= bed < wake <= observed_at")
                onset = _float(raw["bed_offset"], "bed_offset")
                final_wake = _float(raw["wake_offset"], "wake_offset")
                assert onset is not None and final_wake is not None
                if not (0 <= onset <= 960 and onset + 30 <= final_wake <= 1440
                        and final_wake - onset <= 960):
                    raise ValueError("target offsets outside contract")
                timestamp_onset = (bed - cutoff).total_seconds() / 60
                timestamp_wake = (wake - cutoff).total_seconds() / 60
                if abs(timestamp_onset - onset) > 1e-7 or abs(timestamp_wake - final_wake) > 1e-7:
                    raise ValueError("target offsets do not match bed/wake timestamps")
                reference = {
                    name: _float(raw.get(name, ""), name, optional=True)
                    for name in REFERENCE_COLUMNS
                }
                rare_text = (raw.get("rare_by_train_mad") or "").strip().lower()
                rare = None if not rare_text else rare_text in {"1", "true", "yes"}
                age = _float(raw.get("age", ""), "age", optional=True)
                if age is not None and not 0 <= age <= 120:
                    raise ValueError("age outside encoded valid range")
                sex_raw = (raw.get("sex") or "").strip().lower()
                sex = sex_raw if sex_raw in {"female", "male"} else ""
                rows.append({
                    "cohort": cohort, "split": split, "participant_id": participant,
                    "row_id": raw_row_id, "cutoff": cutoff,
                    "bed": bed, "wake": wake, "observed_at": observed,
                    "bed_offset": onset, "wake_offset": final_wake,
                    "age": age, "sex": sex,
                    "label_source": (raw.get("label_source") or "").strip(),
                    "rare_by_train_mad": rare, "reference": reference,
                })
            except (ValueError, TypeError, KeyError) as error:
                failures.append({"stage": "input_validation", "line": line_number,
                                 "reason": str(error)})
    if not rows:
        raise ValueError("Input CSV contains no valid rows")
    raw_overlap = sum(len(splits) > 1 for splits in raw_split_by_person.values())
    if raw_overlap:
        raise ValueError(f"Participant-disjoint split violation for {raw_overlap} participant(s) in raw input")
    if len(raw_row_ids) != len(set(raw_row_ids)):
        raise ValueError("Duplicate non-empty row_id within cohort in raw input")
    identities = [(r["cohort"], r["participant_id"], r["cutoff"]) for r in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate cohort/participant/cutoff rows")
    _validate_demographic_consistency(rows)
    return rows, failures, columns


def _predict_rows(
    rows: list[dict[str, Any]], models: dict[str, Any], policy: str,
    progress: Callable[[dict[str, Any]], None] | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["cohort"], row["participant_id"])].append(row)
    predictions: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    completed = 0
    total = len(rows)
    for (cohort, participant), subject_rows in sorted(grouped.items()):
        subject_rows.sort(key=lambda item: item["cutoff"])
        enrollment = subject_rows[0]["cutoff"].isoformat()
        history: list[dict[str, str]] = []
        for row in subject_rows:
            _check_cancelled(cancelled)
            try:
                result = models[cohort].predict(
                    {"created_at": enrollment}, history, row["cutoff"],
                    personalization_policy=policy,
                )
                feature = result["features"]
                state = result["personalization"]
                feature_missing_count = sum(
                    value is None or not math.isfinite(float(value)) for value in feature.values()
                )
                arm_values = {
                    "data": result["data"], "hybrid": result["hybrid"],
                    "physical": result["physical"],
                    "recent_median": (feature["median14_bed"], feature["median14_wake"]),
                }
                for arm, (predicted_bed, predicted_wake) in arm_values.items():
                    predictions.append({
                        "cohort": cohort, "participant_id": participant,
                        "split": row["split"], "row_id": row["row_id"],
                        "cutoff": row["cutoff"].isoformat(), "policy": policy, "arm": arm,
                        "bed_offset": row["bed_offset"], "wake_offset": row["wake_offset"],
                        "predicted_bed_offset": float(predicted_bed),
                        "predicted_wake_offset": float(predicted_wake),
                        "history_count": int(state["valid_days"]),
                        "personal_weight": float(state["personal_weight"]),
                        "rare_by_train_mad": row["rare_by_train_mad"],
                        "age": row["age"], "sex": row["sex"],
                        "feature_count": len(feature),
                        "feature_missing_count": feature_missing_count,
                    })
            except Exception as error:  # bounded row-level failure is reported, not hidden
                failures.append({"stage": "prediction", "cohort": cohort,
                                 "split": row["split"], "policy": policy,
                                 "reason": "prediction_failed",
                                 "exception_type": type(error).__name__})
            finally:
                # The current target becomes history only after its forecast.
                history.append({
                    "source": "research", "bed": row["bed"].isoformat(),
                    "wake": row["wake"].isoformat(),
                    "observed_at": row["observed_at"].isoformat(),
                })
                completed += 1
                if completed % 250 == 0 or completed == total:
                    _emit(progress, f"replay_{policy}", completed / total)
    return predictions, failures


def _errors(row: dict[str, Any]) -> dict[str, float]:
    onset_residual = row["predicted_bed_offset"] - row["bed_offset"]
    wake_residual = row["predicted_wake_offset"] - row["wake_offset"]
    actual_interval = row["wake_offset"] - row["bed_offset"]
    predicted_interval = row["predicted_wake_offset"] - row["predicted_bed_offset"]
    return {
        "onset": abs(onset_residual), "wake": abs(wake_residual),
        "joint": (abs(onset_residual) + abs(wake_residual)) / 2,
        "interval": abs(predicted_interval - actual_interval),
    }


def _cluster_values(rows: list[dict[str, Any]], metric: str) -> dict[str, list[float]]:
    values: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        values[row["participant_id"]].append(_errors(row)[metric])
    return values


def participant_cluster_interval(
    clusters: dict[str, list[float]], weighting: str, replicates: int, seed: int,
) -> tuple[float, float]:
    """Deterministic percentile CI from participant-cluster resampling."""
    if weighting not in WEIGHTINGS:
        raise ValueError("Unknown weighting")
    if replicates < 1:
        raise ValueError("bootstrap_replicates must be positive")
    keys = sorted(clusters)
    if not keys:
        raise ValueError("No clusters")
    rng = random.Random(seed)
    estimates = []
    for _ in range(replicates):
        sampled = [keys[rng.randrange(len(keys))] for _ in keys]
        if weighting == "participant":
            estimates.append(statistics.fmean(statistics.fmean(clusters[key]) for key in sampled))
        else:
            values = [value for key in sampled for value in clusters[key]]
            estimates.append(statistics.fmean(values))
    estimates.sort()
    low = estimates[max(0, math.floor(0.025 * (replicates - 1)))]
    high = estimates[min(replicates - 1, math.ceil(0.975 * (replicates - 1)))]
    return low, high


def _mean_from_clusters(clusters: dict[str, list[float]], weighting: str) -> float:
    if weighting == "participant":
        return statistics.fmean(statistics.fmean(values) for values in clusters.values())
    return statistics.fmean(value for values in clusters.values() for value in values)


def _metric_records(
    predictions: list[dict[str, Any]], replicates: int, seed: int,
) -> list[dict[str, Any]]:
    records = []
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in predictions:
        groups[(row["cohort"], row["split"], row["policy"], row["arm"])].append(row)
    for (cohort, split, policy, arm), rows in sorted(groups.items()):
        for endpoint_number, metric in enumerate(ENDPOINTS):
            clusters = _cluster_values(rows, metric)
            for weighting_number, weighting in enumerate(WEIGHTINGS):
                ci = (None, None)
                if split == "test":
                    ci = participant_cluster_interval(
                        clusters, weighting, replicates,
                        seed + COHORTS.index(cohort) * 1000
                        + ("train", "validation", "test").index(split) * 100
                        + endpoint_number * 10 + weighting_number,
                    )
                records.append({
                    "cohort": cohort, "split": split, "evaluation_role": (
                        "reused_holdout" if split == "test" else "descriptive_non_holdout"
                    ), "policy": policy, "arm": arm, "weighting": weighting,
                    "metric": f"{metric}_mae_min", "value": _mean_from_clusters(clusters, weighting),
                    "ci95_low": ci[0], "ci95_high": ci[1],
                    "bootstrap_unit": "participant" if split == "test" else None,
                    "bootstrap_replicates": replicates if split == "test" else 0,
                    "n_nights": len(rows), "n_participants": len(clusters),
                })
    return records


def _paired_differences(
    predictions: list[dict[str, Any]], replicates: int, seed: int,
) -> list[dict[str, Any]]:
    by_key = {(r["cohort"], r["row_id"], r["arm"], r["policy"]): r for r in predictions
              if r["split"] == "test"}
    output = []
    for cohort in COHORTS:
        for arm_number, arm in enumerate(ARMS):
            pairs = []
            for key, gradual in by_key.items():
                if key[0] != cohort or key[2] != arm or key[3] != POLICY:
                    continue
                legacy = by_key.get((cohort, key[1], arm, COMPARATOR))
                if legacy:
                    pairs.append((gradual, legacy))
            if not pairs:
                continue
            for metric_number, metric in enumerate(ENDPOINTS):
                clusters: dict[str, list[float]] = defaultdict(list)
                for gradual, legacy in pairs:
                    # Positive means lower error for gradual_v1.
                    clusters[gradual["participant_id"]].append(
                        _errors(legacy)[metric] - _errors(gradual)[metric]
                    )
                for weighting_number, weighting in enumerate(WEIGHTINGS):
                    low, high = participant_cluster_interval(
                        clusters, weighting, replicates,
                        seed + 10000 + arm_number * 100 + metric_number * 10 + weighting_number,
                    )
                    output.append({
                        "cohort": cohort, "split": "test", "arm": arm,
                        "metric": f"{metric}_mae_reduction_min", "weighting": weighting,
                        "value": _mean_from_clusters(clusters, weighting),
                        "ci95_low": low, "ci95_high": high,
                        "positive_means": "lower error for gradual_v1",
                        "bootstrap_unit": "participant", "bootstrap_replicates": replicates,
                        "n_nights": len(pairs), "n_participants": len(clusters),
                    })
    return output


def _weighted_pairs(rows: list[dict[str, Any]], endpoint: str, weighting: str):
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["participant_id"]] += 1
    result = []
    for row in rows:
        if endpoint == "onset":
            actual, predicted = row["bed_offset"], row["predicted_bed_offset"]
        elif endpoint == "wake":
            actual, predicted = row["wake_offset"], row["predicted_wake_offset"]
        else:
            actual = row["wake_offset"] - row["bed_offset"]
            predicted = row["predicted_wake_offset"] - row["predicted_bed_offset"]
        weight = 1.0 if weighting == "night" else 1.0 / counts[row["participant_id"]]
        result.append((actual, predicted, weight))
    return result


def _diagnostics(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in predictions:
        if row["split"] == "test":
            groups[(row["cohort"], row["policy"], row["arm"])].append(row)
    output = []
    for (cohort, policy, arm), rows in sorted(groups.items()):
        for endpoint in ("onset", "wake", "interval"):
            for weighting in WEIGHTINGS:
                pairs = _weighted_pairs(rows, endpoint, weighting)
                total_w = sum(w for _, _, w in pairs)
                actual_mean = sum(a * w for a, _, w in pairs) / total_w
                predicted_mean = sum(p * w for _, p, w in pairs) / total_w
                residuals = [(p - a, w) for a, p, w in pairs]
                bias = sum(r * w for r, w in residuals) / total_w
                rmse = math.sqrt(sum(r * r * w for r, w in residuals) / total_w)
                variance = sum((p - predicted_mean) ** 2 * w for _, p, w in pairs)
                covariance = sum((p - predicted_mean) * (a - actual_mean) * w for a, p, w in pairs)
                slope = covariance / variance if variance > 1e-12 else None
                intercept = actual_mean - slope * predicted_mean if slope is not None else None
                actual_var = sum((a - actual_mean) ** 2 * w for a, _, w in pairs)
                correlation = (
                    covariance / math.sqrt(variance * actual_var)
                    if variance > 1e-12 and actual_var > 1e-12 else None
                )
                output.append({
                    "cohort": cohort, "split": "test", "policy": policy, "arm": arm,
                    "endpoint": endpoint, "weighting": weighting, "residual_bias_min": bias,
                    "rmse_min": rmse, "calibration_intercept_min": intercept,
                    "calibration_slope": slope, "pearson_correlation": correlation,
                    "n_nights": len(rows),
                    "n_participants": len({row["participant_id"] for row in rows}),
                })
    return output


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Cannot compute a quantile without values")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _empirical_intervals(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fix validation residual quantiles, then apply them unchanged to reused Test."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in predictions:
        groups[(row["cohort"], row["policy"], row["arm"])].append(row)
    output = []
    for (cohort, policy, arm), rows in sorted(groups.items()):
        validation = [row for row in rows if row["split"] == "validation"]
        test = [row for row in rows if row["split"] == "test"]
        if not validation or not test:
            output.append({
                "cohort": cohort, "policy": policy, "arm": arm,
                "status": "unavailable_missing_validation_or_test_rows",
                "n_validation_nights": len(validation), "n_test_nights": len(test),
            })
            continue
        for endpoint in ("onset", "wake"):
            actual_key = "bed_offset" if endpoint == "onset" else "wake_offset"
            predicted_key = "predicted_bed_offset" if endpoint == "onset" else "predicted_wake_offset"
            residuals = [row[actual_key] - row[predicted_key] for row in validation]
            low = _quantile(residuals, 0.025)
            high = _quantile(residuals, 0.975)
            covered_by_person: dict[str, list[float]] = defaultdict(list)
            for row in test:
                lower = row[predicted_key] + low
                upper = row[predicted_key] + high
                covered_by_person[row["participant_id"]].append(
                    float(lower <= row[actual_key] <= upper)
                )
            for weighting in WEIGHTINGS:
                coverage = _mean_from_clusters(covered_by_person, weighting)
                output.append({
                    "cohort": cohort, "policy": policy, "arm": arm, "endpoint": endpoint,
                    "weighting": weighting, "calibration_split": "validation",
                    "evaluation_split": "test", "residual_quantile_low_min": low,
                    "residual_quantile_high_min": high, "mean_interval_width_min": high - low,
                    "empirical_coverage": coverage, "nominal_quantiles": [0.025, 0.975],
                    "n_validation_nights": len(validation),
                    "n_validation_participants": len({r['participant_id'] for r in validation}),
                    "n_test_nights": len(test), "n_test_participants": len(covered_by_person),
                    "status": "exploratory_reused_data_empirical_interval",
                    "guarantee": "not conformal; not a guaranteed individual 95% prediction interval; no deployment",
                })
    return output


def _predictor_missingness(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Report replay-computed feature missingness once per row (data arm only)."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in predictions:
        if row["arm"] == "data":
            groups[(row["cohort"], row["split"], row["policy"])].append(row)
    output = []
    for (cohort, split, policy), rows in sorted(groups.items()):
        output.append({
            "cohort": cohort, "split": split, "policy": policy,
            "n_rows": len(rows), "computed_features_per_row": rows[0]["feature_count"],
            "rows_with_any_missing_computed_feature": sum(r["feature_missing_count"] > 0 for r in rows),
            "total_missing_computed_feature_values": sum(r["feature_missing_count"] for r in rows),
            "note": "Counts cover replay-computed portable feature values before frozen model preprocessing.",
        })
    return output


def _summaries(rows: list[dict[str, Any]], columns: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    flow = []
    distributions = []
    for cohort in COHORTS:
        cohort_rows = [r for r in rows if r["cohort"] == cohort]
        for split in ("train", "validation", "test"):
            selected = [r for r in cohort_rows if r["split"] == split]
            if not selected:
                continue
            ages = [r["age"] for r in selected if r["age"] is not None]
            sex_counts: dict[str, int] = defaultdict(int)
            for row in selected:
                sex_counts[row["sex"] or "missing"] += 1
            participant_demographics = {}
            for row in selected:
                participant_demographics.setdefault(
                    row["participant_id"], {"age": row["age"], "sex": row["sex"]}
                )
            participant_ages = [item["age"] for item in participant_demographics.values()
                                if item["age"] is not None]
            participant_sex_counts: dict[str, int] = defaultdict(int)
            for item in participant_demographics.values():
                participant_sex_counts[item["sex"] or "missing"] += 1
            flow.append({
                "cohort": cohort, "split": split, "n_rows": len(selected),
                "n_participants": len({r["participant_id"] for r in selected}),
                "age_missing_rows": len(selected) - len(ages),
                "sex_missing_rows": sum(not r["sex"] for r in selected),
                "age_mean": statistics.fmean(ages) if ages else None,
                "age_min": min(ages) if ages else None, "age_max": max(ages) if ages else None,
                "sex_counts_by_row": dict(sorted(sex_counts.items())),
                "age_missing_participants": len(participant_demographics) - len(participant_ages),
                "sex_missing_participants": sum(not item["sex"] for item in participant_demographics.values()),
                "participant_age_mean": statistics.fmean(participant_ages) if participant_ages else None,
                "participant_age_min": min(participant_ages) if participant_ages else None,
                "participant_age_max": max(participant_ages) if participant_ages else None,
                "sex_counts_by_participant": dict(sorted(participant_sex_counts.items())),
                "label_sources": sorted({r["label_source"] for r in selected if r["label_source"]}),
            })
            for field in ("bed_offset", "wake_offset"):
                values = [float(r[field]) for r in selected]
                distributions.append({
                    "cohort": cohort, "split": split, "field": field,
                    "n": len(values), "missing": 0, "mean": statistics.fmean(values),
                    "sd": statistics.pstdev(values), "min": min(values), "max": max(values),
                })
    return flow, distributions


def _validate_demographic_consistency(rows: list[dict[str, Any]]) -> None:
    values: dict[tuple[str, str], dict[str, set[Any]]] = defaultdict(
        lambda: {"age": set(), "sex": set()}
    )
    for row in rows:
        key = (row["cohort"], row["participant_id"])
        if row["age"] is not None:
            values[key]["age"].add(row["age"])
        if row["sex"]:
            values[key]["sex"].add(row["sex"])
    inconsistent = sum(
        len(item["age"]) > 1 or len(item["sex"]) > 1 for item in values.values()
    )
    if inconsistent:
        raise ValueError(f"Inconsistent within-participant demographics for {inconsistent} participant(s)")


def _load_source_metadata(input_path: Path, rows: list[dict[str, Any]]) -> tuple[dict[str, Any], Path | None]:
    metadata_path = input_path.with_suffix(".metadata.json")
    if not metadata_path.is_file():
        return {"available": False, "reason": "normalized input metadata sidecar not supplied"}, None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("schema") != "sleepwell.gradual_input_metadata.v1":
        raise ValueError("Input metadata sidecar schema is incompatible")
    normalized = metadata.get("normalized_input")
    if not isinstance(normalized, dict) or normalized.get("sha256") != _sha256(input_path):
        raise ValueError("Input metadata sidecar CSV hash mismatch")
    if normalized.get("rows") != len(rows):
        raise ValueError("Input metadata sidecar row count mismatch")
    expected = {}
    for cohort in COHORTS:
        cohort_rows = [row for row in rows if row["cohort"] == cohort]
        expected[cohort] = {
            "rows": len(cohort_rows),
            "participants": len({row["participant_id"] for row in cohort_rows}),
            "split_rows": {split: sum(row["split"] == split for row in cohort_rows)
                           for split in ("test", "train", "validation")},
        }
    if metadata.get("cohorts") != expected:
        raise ValueError("Input metadata sidecar cohort/split counts mismatch")
    for source in metadata.get("sources", []):
        if not isinstance(source, dict) or not source.get("path") or not source.get("sha256"):
            raise ValueError("Input metadata source hash declaration is malformed")
        source_path = Path(source["path"])
        if not source_path.is_file() or _sha256(source_path) != source["sha256"]:
            raise ValueError(f"Input metadata declared source failed hash verification: {source.get('role', 'unknown')}")
    return metadata, metadata_path


def _stratified(predictions: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in predictions:
        if row["split"] != "test":
            continue
        if field == "history":
            count = row["history_count"]
            label = "0" if count == 0 else "1-6" if count <= 6 else "7-13" if count <= 13 else "14+"
        elif field == "rarity":
            if row["rare_by_train_mad"] is None:
                continue
            label = "statistically_rare" if row["rare_by_train_mad"] else "other"
        elif field == "sex":
            if not row["sex"]:
                continue
            label = row["sex"]
        elif field == "age":
            if row["age"] is None:
                continue
            label = "0-64" if row["age"] < 65 else "65-74" if row["age"] < 75 else "75+"
        else:
            raise ValueError("Unknown stratum")
        groups[(row["cohort"], row["policy"], row["arm"], label)].append(row)
    output = []
    for (cohort, policy, arm, label), rows in sorted(groups.items()):
        clusters = _cluster_values(rows, "joint")
        output.append({
            "cohort": cohort, "split": "test", "policy": policy, "arm": arm,
            "stratum_type": field, "stratum": label,
            "joint_mae_min_participant_weighted": _mean_from_clusters(clusters, "participant"),
            "n_nights": len(rows), "n_participants": len(clusters),
            "very_small_group_caution": len(clusters) < 20,
        })
    return output


def _verify_parity(rows: list[dict[str, Any]], legacy: list[dict[str, Any]]) -> dict[str, Any]:
    source = {(r["cohort"], r["row_id"]): r for r in rows if r["split"] == "test"}
    predicted = {(r["cohort"], r["row_id"], r["arm"]): r for r in legacy
                 if r["split"] == "test" and r["arm"] in {"data", "hybrid", "physical"}}
    cohorts = {}
    for cohort in COHORTS:
        cohort_rows = [r for key, r in source.items() if key[0] == cohort]
        arm_result = {}
        for arm in ("data", "hybrid", "physical"):
            maximum = 0.0
            covered = 0
            for row in cohort_rows:
                reference_bed = row["reference"][f"legacy_{arm}_bed"]
                reference_wake = row["reference"][f"legacy_{arm}_wake"]
                replayed = predicted.get((cohort, row["row_id"], arm))
                if reference_bed is None or reference_wake is None or replayed is None:
                    continue
                maximum = max(maximum, abs(reference_bed - replayed["predicted_bed_offset"]),
                              abs(reference_wake - replayed["predicted_wake_offset"]))
                covered += 1
            arm_result[arm] = {"expected_test_rows": len(cohort_rows), "covered_rows": covered,
                               "max_abs_error_min": maximum, "tolerance_min": PARITY_TOLERANCE_MIN,
                               "verified": covered == len(cohort_rows) and maximum <= PARITY_TOLERANCE_MIN}
            if not arm_result[arm]["verified"]:
                raise ValueError(f"Frozen legacy replay parity failed: {cohort}/{arm}")
        cohorts[cohort] = arm_result
    return {"verified": True, "policy": COMPARATOR, "cohorts": cohorts}


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = sorted({key for record in records for key in record})
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow(record)


def _write_report(path: Path, evaluation: dict[str, Any]) -> None:
    lookup = {(r["cohort"], r["policy"], r["arm"], r["weighting"], r["metric"]): r
              for r in evaluation["metrics"] if r["split"] == "test"}
    lines = [
        "# Frozen gradual_v1 retrospective reassessment", "",
        f"Status: `{STATUS}`. This is a reused holdout analysis, not fresh external or prospective validation.", "",
        "## Primary reused-Test results", "",
        "| Cohort | Policy | Arm | Participant-weighted joint MAE, min (95% cluster CI) | Nights | Participants |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for cohort in COHORTS:
        for policy in (COMPARATOR, POLICY):
            for arm in ARMS:
                row = lookup.get((cohort, policy, arm, "participant", "joint_mae_min"))
                if row is None:
                    continue
                ci = f"{row['value']:.2f} ({row['ci95_low']:.2f}, {row['ci95_high']:.2f})"
                lines.append(f"| {cohort.upper()} | {policy} | {arm} | {ci} | {row['n_nights']} | {row['n_participants']} |")
    lines.extend([
        "", "## Interpretation boundary", "",
        "The policy comparison changes inference-time personalization only. Model parameters, cohort splits, and target rows are frozen.",
        "Targets are cohort-specific event/ACT main-sleep timing, not PSG sleep stages or demonstrated clinical benefit.",
        "Demographic generalization, true production delivery timestamps, prospective performance, and clinical utility remain unverified.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def evaluate(
    input_csv: str | os.PathLike[str], output_dir: str | os.PathLike[str],
    bootstrap_replicates: int = 1000, seed: int = 20261103,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Evaluate gradual_v1 against explicit legacy14 and write immutable outputs.

    ``input_csv`` follows ``scripts/reevaluate_gradual.py --prepare``.  The output
    directory must not exist or must be empty; existing evidence is never overwritten.
    """
    input_path = Path(input_csv).resolve()
    destination = Path(output_dir).resolve()
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"Output directory is not empty: {destination}")
    if bootstrap_replicates < 1:
        raise ValueError("bootstrap_replicates must be positive")
    _check_cancelled(cancelled)
    _emit(progress, "read_input", 0.0)
    rows, failures, columns = _read_input(input_path)
    if failures:
        reasons: dict[str, int] = defaultdict(int)
        for failure in failures:
            reasons[failure["reason"]] += 1
        raise ValueError(
            f"Input validation failed closed for {len(failures)} row(s): "
            + "; ".join(f"{reason} ({count})" for reason, count in sorted(reasons.items()))
        )
    source_metadata, metadata_path = _load_source_metadata(input_path, rows)
    _emit(progress, "read_input", 1.0)

    fusion = vendor_module("fusion_forecast")
    model_dir = Path(fusion.__file__).resolve().parent / "models"
    models = {cohort: fusion.PortableCohortModel(model_dir / f"robust_{cohort}_v2.json", cohort)
              for cohort in COHORTS}
    legacy, legacy_failures = _predict_rows(rows, models, COMPARATOR, progress, cancelled)
    failures.extend(legacy_failures)
    parity = _verify_parity(rows, legacy)
    _check_cancelled(cancelled)
    gradual, gradual_failures = _predict_rows(rows, models, POLICY, progress, cancelled)
    failures.extend(gradual_failures)
    predictions = legacy + gradual
    expected = len(rows) * len(ARMS) * 2
    if len(predictions) != expected:
        raise ValueError(f"Prediction coverage incomplete: {len(predictions)}/{expected}")

    _emit(progress, "statistics", 0.0)
    metrics = _metric_records(predictions, bootstrap_replicates, seed)
    differences = _paired_differences(predictions, bootstrap_replicates, seed)
    flow, distributions = _summaries(rows, columns)
    module_path = Path(__file__).resolve()
    sources = [
        {"role": "normalized_private_input", "path": str(input_path), "sha256": _sha256(input_path)},
        {"role": "evaluation_code", "path": str(module_path), "sha256": _sha256(module_path)},
    ]
    if metadata_path is not None:
        sources.append({"role": "input_metadata", "path": str(metadata_path), "sha256": _sha256(metadata_path)})
    for cohort, model in models.items():
        sources.append({"role": f"frozen_model_{cohort}", "path": str(model.path), "sha256": model.sha256})
    vendor_provenance = get_provenance()
    evaluation = {
        "schema": SCHEMA, "policy": POLICY, "comparator": COMPARATOR,
        "comparator_policy": COMPARATOR, "status": STATUS,
        "evaluation_status": STATUS, "seed": seed,
        "bootstrap_replicates": bootstrap_replicates,
        "modeling": {
            "parameters": "frozen robust-v2 portable parameters; no refit or selection",
            "history_rule": "only completed records with wake and observed_at strictly before cutoff; current label appended after prediction",
            "enrollment_rule": "first cutoff across all rows for each participant",
            "split_rule": "existing participant-disjoint CMI/MESA splits",
        },
        "legacy_replay_parity": parity, "metrics": metrics,
        "paired_policy_differences": differences,
        "diagnostics": _diagnostics(predictions),
        "exploratory_residual_intervals": _empirical_intervals(predictions),
        "history_strata": _stratified(predictions, "history"),
        "rarity_strata": _stratified(predictions, "rarity"),
        "demographic_strata": _stratified(predictions, "sex") + _stratified(predictions, "age"),
        "cohort_flow": flow, "split_distributions": distributions,
        "predictor_missingness": _predictor_missingness(predictions),
        "source_qc": source_metadata, "failures": failures,
        "failure_summary": {"count": len(failures),
                            "evaluable_input_rows": len(rows),
                            "prediction_records": len(predictions)},
        "sources": sources, "verified_vendor_provenance": vendor_provenance,
        "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                        "platform": platform.platform(), "executable": sys.executable},
        "limitations": [
            "Retrospective adaptive reassessment on previously exposed reused holdouts; not fresh external confirmation.",
            "CMI event-derived and MESA weak ACT-derived timing targets are not PSG sleep stages.",
            "No true production forecast-delivery timestamps are available; 20:00 cutoffs are reconstructed research rows.",
            "CMI age and sex are unavailable; MESA encoded age/sex strata are descriptive and small groups require caution.",
            "Validation-residual empirical intervals use reused data and are not conformal or guaranteed individual 95% intervals.",
            "Bootstrap intervals are conditional on frozen model parameters and do not include training or model-selection uncertainty.",
            "No treatment effect, clinical utility, or sleep benefit is estimated.",
        ],
        "privacy": "Aggregate JSON/CSV/report omit participant identifiers; private_predictions.csv contains local row evidence.",
    }
    _emit(progress, "statistics", 1.0)
    _check_cancelled(cancelled)

    destination.mkdir(parents=True, exist_ok=True)
    aggregate_path = destination / "evaluation.json"
    aggregate_path.write_text(json.dumps(evaluation, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                              encoding="utf-8")
    _write_csv(destination / "metrics.csv", metrics + differences)
    _write_report(destination / "report.md", evaluation)
    private_fields = [
        "cohort", "participant_id", "split", "row_id", "cutoff", "policy", "arm",
        "bed_offset", "wake_offset", "predicted_bed_offset", "predicted_wake_offset",
        "history_count", "personal_weight", "rare_by_train_mad",
    ]
    with (destination / "private_predictions.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=private_fields)
        writer.writeheader()
        writer.writerows({name: row.get(name) for name in private_fields} for row in predictions)
    _emit(progress, "complete", 1.0)
    return {"result_path": str(aggregate_path), "evaluation": evaluation,
            "private_result_path": str(destination / "private_predictions.csv")}

