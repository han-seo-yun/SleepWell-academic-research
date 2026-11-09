"""Research-only sensor observation policy for parallel Process S reconstruction.

This module never selects the official forecast path. It accepts only causal,
completed five-minute observations and keeps explicit bounds across missing
support instead of inventing sleep or wake states.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta

POLICY_VERSION = "research_shadow_sensor_s_v1"


def _probability_update(advance_state, state, probability, hours):
    sleep_state = advance_state(state, True, hours)
    wake_state = advance_state(state, False, hours)
    return probability * sleep_state + (1 - probability) * wake_state


def compute_parallel_process_s(
    observations,
    cutoff,
    diary_s,
    diary_state_at,
    advance_state,
    normalize_time,
    uncertainty_threshold,
):
    """Compute diary, hard-sensor, and calibrated-probability S in parallel.

    ``diary_state_at`` supplies the shared anchor S at the first valid sensor
    window. Once support has a gap, sensor paths remain interval-valued; no
    midpoint, forward fill, sleep assumption, or wake assumption is selected.
    """
    threshold = float(uncertainty_threshold)
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("Invalid sensor Process S uncertainty threshold")
    cutoff = normalize_time(cutoff)
    observations = list(observations)
    canonical = json.dumps(
        observations, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    audit = {
        "policy": POLICY_VERSION,
        "input_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "observations_received": len(observations),
        "observations_used": 0,
        "observations_excluded": [],
        "gaps": [],
        "uncertainty_threshold": threshold,
        "official_forecast_unchanged": True,
    }
    parsed = []
    for index, row in enumerate(observations):
        try:
            start = normalize_time(row["start"])
            end = normalize_time(row["end"])
            available = normalize_time(row["available_at"])
            binary_value = float(row["sleep_binary"])
            if binary_value not in (0.0, 1.0):
                raise ValueError("binary_not_zero_or_one")
            binary = int(binary_value)
            probability = float(row["sleep_probability"])
            calibrated = row["probability_calibrated"] is True
            model_id = row["observation_model_id"]
            calibration_id = row.get("calibration_id")
            if not isinstance(model_id, str) or not model_id.strip():
                raise ValueError("missing_observation_model_id")
            if calibrated and (not isinstance(calibration_id, str) or not calibration_id.strip()):
                raise ValueError("missing_calibration_id")
            if end - start != timedelta(minutes=5):
                raise ValueError("window_not_five_minutes")
            if binary not in (0, 1):
                raise ValueError("binary_not_zero_or_one")
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("probability_out_of_range")
            if available < end:
                raise ValueError("available_before_window_end")
            if end > cutoff or available > cutoff:
                raise ValueError("not_available_by_cutoff")
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            audit["observations_excluded"].append({"index": index, "reason": str(error)})
            continue
        parsed.append({
            "index": index,
            "start": start,
            "end": end,
            "available_at": available,
            "sleep_binary": binary,
            "sleep_probability": probability,
            "probability_calibrated": calibrated,
            "observation_model_id": model_id,
            "calibration_id": calibration_id,
        })
    parsed.sort(key=lambda row: (row["start"], row["end"], row["index"]))
    for previous, current in zip(parsed, parsed[1:]):
        if current["start"] < previous["end"]:
            raise ValueError("Sensor observation windows overlap")
    model_ids = sorted({row["observation_model_id"] for row in parsed})
    calibration_ids = sorted({row["calibration_id"] for row in parsed if row["probability_calibrated"]})
    if len(model_ids) > 1 or len(calibration_ids) > 1:
        raise ValueError("Sensor observation provenance changes within a reconstruction")
    audit["observation_model_ids"] = model_ids
    audit["calibration_ids"] = calibration_ids
    if not parsed:
        return _diary_fallback(diary_s, audit, ["no_causal_observations"])

    anchor = parsed[0]["start"]
    anchor_s = float(diary_state_at(anchor))
    if not math.isfinite(anchor_s) or not 0 <= anchor_s <= 1:
        raise ValueError("Invalid diary anchor S")
    states = {
        "S_sensor_binary": {"lower": anchor_s, "upper": anchor_s, "point": anchor_s},
        "S_sensor_probability": {"lower": anchor_s, "upper": anchor_s, "point": anchor_s},
    }
    probability_admissible = True
    cursor = anchor
    for row in parsed:
        if row["start"] > cursor:
            _propagate_gap(states, cursor, row["start"], advance_state, audit)
        hours = (row["end"] - row["start"]).total_seconds() / 3600
        binary = states["S_sensor_binary"]
        binary["lower"] = advance_state(binary["lower"], bool(row["sleep_binary"]), hours)
        binary["upper"] = advance_state(binary["upper"], bool(row["sleep_binary"]), hours)
        if binary["point"] is not None:
            binary["point"] = advance_state(binary["point"], bool(row["sleep_binary"]), hours)
        probability = states["S_sensor_probability"]
        probability["lower"] = _probability_update(
            advance_state, probability["lower"], row["sleep_probability"], hours)
        probability["upper"] = _probability_update(
            advance_state, probability["upper"], row["sleep_probability"], hours)
        if probability["point"] is not None:
            probability["point"] = _probability_update(
                advance_state, probability["point"], row["sleep_probability"], hours)
        probability_admissible = probability_admissible and row["probability_calibrated"]
        cursor = row["end"]
        audit["observations_used"] += 1
    if cursor < cutoff:
        _propagate_gap(states, cursor, cutoff, advance_state, audit)

    paths = {"S_diary": {"s": float(diary_s), "status": "available"}}
    for name, state in states.items():
        lower, upper = sorted((float(state["lower"]), float(state["upper"])))
        width = upper - lower
        status = "available" if state["point"] is not None else "bounded_only"
        if name == "S_sensor_probability" and not probability_admissible:
            status = "uncalibrated_probability"
        paths[name] = {
            "s": float(state["point"]) if status == "available" else None,
            "bounds": [lower, upper],
            "uncertainty_width": width,
            "uncertainty_exceeds_threshold": width > threshold,
            "status": status,
        }

    probability = paths["S_sensor_probability"]
    reasons = []
    if probability["status"] == "uncalibrated_probability":
        reasons.append("probability_not_validation_calibrated")
    if audit["gaps"]:
        reasons.append("noncontinuous_sensor_support")
    if probability["uncertainty_exceeds_threshold"]:
        reasons.append("sensor_uncertainty_exceeds_threshold")
    selected = "S_sensor_probability" if not reasons and probability["s"] is not None else "S_diary"
    if selected == "S_diary" and not reasons:
        reasons.append("sensor_probability_unavailable")
    return {
        "policy": POLICY_VERSION,
        "official_path": "S_diary",
        "official_s": float(diary_s),
        "research_selected_path": selected,
        "fallback_used": selected == "S_diary",
        "fallback_reasons": reasons,
        "paths": paths,
        "audit": audit,
    }


def _propagate_gap(states, start, end, advance_state, audit):
    hours = (end - start).total_seconds() / 3600
    if hours <= 0:
        return
    audit["gaps"].append({
        "start": start.isoformat(),
        "end": end.isoformat(),
        "minutes": hours * 60,
        "reason": "missing_or_unusable_observation",
    })
    for state in states.values():
        state["lower"] = advance_state(state["lower"], True, hours)
        state["upper"] = advance_state(state["upper"], False, hours)
        state["point"] = None


def _diary_fallback(diary_s, audit, reasons):
    return {
        "policy": POLICY_VERSION,
        "official_path": "S_diary",
        "official_s": float(diary_s),
        "research_selected_path": "S_diary",
        "fallback_used": True,
        "fallback_reasons": list(reasons),
        "paths": {
            "S_diary": {"s": float(diary_s), "status": "available"},
            "S_sensor_binary": {"s": None, "bounds": None, "status": "unavailable"},
            "S_sensor_probability": {"s": None, "bounds": None, "status": "unavailable"},
        },
        "audit": audit,
    }
