"""Comparable regression summaries for SleepWell timing predictions.

The estimand is fixed before aggregation: every selected model is evaluated on
the same participant/cutoff keys.  Participant-equal aggregation first computes
each participant's metric and then gives every participant equal weight.  Its
RMSE is therefore the mean of participant-specific RMSE values, matching the
preserved SleepWell research engine rather than a pooled-night RMSE.
"""
from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean
from typing import Any


TARGET_COLUMNS = {
    "onset": ("actual_onset_offset_min", "onset_offset_min"),
    "wake": ("actual_wake_offset_min", "wake_offset_min"),
    "interval": ("actual_interval_min", "interval_min"),
}


def _predictions(result: dict[str, Any]) -> list[dict[str, Any]]:
    value = result.get("result", result)
    raw = value.get("raw", value)
    rows = raw.get("predictions", []) if isinstance(raw, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _empty(target: str, averaging: str, tolerance: float, model_ids: list[str],
           warning: str, excluded_counts: dict[str, dict[str, int]] | None = None) -> dict[str, Any]:
    return {
        "rows": [], "predictions": [], "participant_rows": [],
        "model_ids": model_ids, "n_nights": 0, "n_participants": 0,
        "excluded_counts": excluded_counts or {}, "warnings": [warning],
        "available": False, "target": target, "averaging": averaging,
        "tolerance": tolerance,
    }


def comparison_data(result: dict[str, Any], model_ids: list[str] | None = None,
                    target: str = "onset", averaging: str = "participant_equal",
                    tolerance: float = 30) -> dict[str, Any]:
    """Return model metrics on identical nights without changing stored output.

    ``within_tolerance`` is the inclusive fraction ``abs(error) <= tolerance``.
    It is a regression tolerance rate, not classification accuracy.  Sleep onset
    and wake values are minute offsets from the prediction cutoff; ``interval``
    is the observed sleep interval and is not physiological total sleep time.
    """
    if target not in TARGET_COLUMNS:
        raise ValueError("target must be one of onset, wake, interval")
    if averaging not in {"participant_equal", "night_equal"}:
        raise ValueError("averaging must be participant_equal or night_equal")
    tolerance_value = _finite(tolerance)
    if tolerance_value is None or tolerance_value < 0:
        raise ValueError("tolerance must be a finite non-negative number")

    source = _predictions(result)
    present = list(dict.fromkeys(str(row.get("model_id")) for row in source if row.get("model_id") is not None))
    selected = list(dict.fromkeys(str(value) for value in (model_ids if model_ids is not None else present)))
    if not source:
        return _empty(target, averaging, tolerance_value, selected,
                      "Prediction rows are unavailable; summary values were not substituted.")
    if not selected:
        return _empty(target, averaging, tolerance_value, selected, "No models were selected.")

    actual_column, predicted_column = TARGET_COLUMNS[target]
    by_model: dict[str, dict[tuple[str, str], tuple[float, float]]] = {}
    excluded: dict[str, dict[str, int]] = {}
    for model_id in selected:
        rows = [row for row in source if str(row.get("model_id")) == model_id]
        mapping: dict[tuple[str, str], tuple[float, float]] = {}
        seen_keys: set[tuple[str, str]] = set()
        invalid = 0
        for row in rows:
            participant = row.get("participant_id")
            cutoff = row.get("cutoff")
            participant_text = str(participant).strip() if participant is not None else ""
            cutoff_text = str(cutoff).strip() if cutoff is not None else ""
            if not participant_text or not cutoff_text:
                invalid += 1
                continue
            key = (participant_text, cutoff_text)
            if key in seen_keys:
                raise ValueError(f"duplicate_prediction_key:{model_id}:{key[0]}:{key[1]}")
            seen_keys.add(key)
            actual = _finite(row.get(actual_column))
            predicted = _finite(row.get(predicted_column))
            if actual is None or predicted is None:
                invalid += 1
                continue
            mapping[key] = (actual, predicted)
        by_model[model_id] = mapping
        excluded[model_id] = {
            "total_rows": len(rows), "invalid_pairs": invalid, "outside_common_nights": 0,
        }

    if any(not by_model[model_id] for model_id in selected):
        for model_id in selected:
            excluded[model_id]["outside_common_nights"] = len(by_model[model_id])
        return _empty(target, averaging, tolerance_value, selected,
                      "At least one selected model has no valid prediction pairs.", excluded)
    common = set.intersection(*(set(by_model[model_id]) for model_id in selected))
    if not common:
        for model_id in selected:
            excluded[model_id]["outside_common_nights"] = len(by_model[model_id])
        return _empty(target, averaging, tolerance_value, selected,
                      "The selected models have no common valid prediction nights.", excluded)
    for model_id in selected:
        excluded[model_id]["outside_common_nights"] = len(by_model[model_id]) - len(common)

    for key in common:
        truths = [by_model[model_id][key][0] for model_id in selected]
        if any(not math.isclose(value, truths[0], rel_tol=0, abs_tol=1e-9) for value in truths[1:]):
            raise ValueError(f"actual_value_mismatch:{key[0]}:{key[1]}")

    normalized: list[dict[str, Any]] = []
    participant_groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for model_id in selected:
        for participant, cutoff in sorted(common):
            actual, predicted = by_model[model_id][(participant, cutoff)]
            error = predicted - actual
            normalized.append({
                "participant_id": participant, "cutoff": cutoff, "model_id": model_id,
                "actual": actual, "predicted": predicted, "error": error,
            })
            participant_groups[(model_id, participant)].append(error)

    participant_rows: list[dict[str, Any]] = []
    for (model_id, participant), errors in sorted(participant_groups.items()):
        participant_rows.append({
            "model_id": model_id, "participant_id": participant,
            "mae": mean(abs(value) for value in errors),
            "rmse": math.sqrt(mean(value * value for value in errors)),
            "bias": mean(errors),
            "within_tolerance": mean(abs(value) <= tolerance_value for value in errors),
            "n_nights": len(errors),
        })

    rows: list[dict[str, Any]] = []
    for model_id in selected:
        model_predictions = [row for row in normalized if row["model_id"] == model_id]
        people = [row for row in participant_rows if row["model_id"] == model_id]
        if averaging == "participant_equal":
            metrics = {key: mean(row[key] for row in people)
                       for key in ("mae", "rmse", "bias", "within_tolerance")}
        else:
            errors = [row["error"] for row in model_predictions]
            metrics = {
                "mae": mean(abs(value) for value in errors),
                "rmse": math.sqrt(mean(value * value for value in errors)),
                "bias": mean(errors),
                "within_tolerance": mean(abs(value) <= tolerance_value for value in errors),
            }
        rows.append({"model_id": model_id, **metrics, "n_nights": len(common),
                     "n_participants": len(people)})

    participants = {participant for participant, _cutoff in common}
    warnings = ["ROC-AUC, sensitivity, and classification accuracy do not apply to these continuous timing predictions."]
    if any(value["invalid_pairs"] or value["outside_common_nights"] for value in excluded.values()):
        warnings.append("Some predictions were excluded so every model uses identical valid nights.")
    return {
        "rows": rows, "predictions": normalized, "participant_rows": participant_rows,
        "model_ids": selected, "n_nights": len(common), "n_participants": len(participants),
        "excluded_counts": excluded, "warnings": warnings, "available": True,
        "target": target, "averaging": averaging, "tolerance": tolerance_value,
    }


__all__ = ["comparison_data", "TARGET_COLUMNS"]
