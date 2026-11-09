"""Causal research adapter for continuously updated personal sleep schedules.

The legacy path is delegated unchanged.  Daily candidates reuse the frozen v1
feature implementation and replace only its personalized reference schedule and
the physical quantities that depend on that schedule.  The fixed two-process
time constants remain engineering constants; this module does not estimate
individual physiology.
"""
from __future__ import annotations

import math
from datetime import timedelta

from night_core import (
    circadian,
    constrain,
    local_time,
    normalize_history,
    physical_prediction,
    reconstruct_s,
    stationary_thresholds,
)
from night_features_v2 import extended_features as legacy_extended_features


POLICY_SPECS = {
    "legacy14": {
        "mode": "legacy_exact",
        "activation": "n>=14 and elapsed_days>=14",
    },
    "daily_mean_h14_l14": {
        "mode": "daily",
        "aggregate": "weighted_mean",
        "half_life_days": 14.0,
        "lambda": 14.0,
        "activation": "first_eligible_record",
        "pattern": False,
    },
    "daily_median_h14_l14": {
        "mode": "daily",
        "aggregate": "weighted_median",
        "half_life_days": 14.0,
        "lambda": 14.0,
        "activation": "first_eligible_record",
        "pattern": False,
    },
    "daily_pattern_h14_l14": {
        "mode": "daily",
        "aggregate": "weighted_median",
        "half_life_days": 14.0,
        "lambda": 14.0,
        "activation": "first_eligible_record",
        "pattern": True,
        "pattern_lambda": 14.0,
        "pattern_weight_cap": 0.25,
        "minimum_each_weektype": 2,
    },
    "daily_mean_h28_l28": {
        "mode": "daily",
        "aggregate": "weighted_mean",
        "half_life_days": 28.0,
        "lambda": 28.0,
        "activation": "first_eligible_record",
        "pattern": False,
    },
}


def _weighted_median(values, weights):
    order = sorted(range(len(values)), key=lambda i: values[i])
    threshold = sum(weights) / 2.0
    cumulative = 0.0
    for index in order:
        cumulative += weights[index]
        if cumulative >= threshold:
            return float(values[index])
    raise ValueError("weighted median requires positive mass")


def _aggregate(values, weights, method):
    if method == "weighted_mean":
        return float(sum(v * w for v, w in zip(values, weights)) / sum(weights))
    if method == "weighted_median":
        return _weighted_median(values, weights)
    raise ValueError(f"Unknown aggregate: {method}")


def _weekend(record):
    return record["wake"].weekday() >= 5


def extended_features(records, cutoff, prior, enrolled_at, policy_name):
    """Return v1 features/state with a predeclared causal personalization policy.

    Availability is determined exclusively by the original ``normalize_history``
    contract: both wake and observed timestamps must precede the forecast cutoff.
    Daily candidates use every eligible normalized record; half-life weights make
    older records progressively less influential without a hard history window.
    """
    if policy_name not in POLICY_SPECS:
        raise ValueError(f"Unknown policy: {policy_name}")

    values, state = legacy_extended_features(records, cutoff, prior, enrolled_at)
    if policy_name == "legacy14":
        return values, state

    cutoff_local = local_time(cutoff)
    history = normalize_history(records, cutoff_local, enrolled_at)
    spec = POLICY_SPECS[policy_name]
    half_life = spec["half_life_days"]
    weights = [2.0 ** (-(cutoff_local - r["wake"]).total_seconds() / 86400.0 / half_life)
               for r in history]
    mass = float(sum(weights))
    weight_sq = float(sum(w * w for w in weights))
    effective_n = mass * mass / weight_sq if weight_sq else 0.0
    personal_weight = mass / (mass + spec["lambda"]) if mass else 0.0
    prior_bed = float(prior["bed_offset"])
    prior_wake = float(prior["wake_offset"])

    if mass > 0.0:
        bed_center = _aggregate([r["bed_offset"] for r in history], weights, spec["aggregate"])
        wake_center = _aggregate([r["wake_offset"] for r in history], weights, spec["aggregate"])
        habit_bed = prior_bed + personal_weight * (bed_center - prior_bed)
        habit_wake = prior_wake + personal_weight * (wake_center - prior_wake)
    else:
        bed_center, wake_center = prior_bed, prior_wake
        habit_bed, habit_wake = prior_bed, prior_wake

    weekdays = [i for i, r in enumerate(history) if not _weekend(r)]
    weekends = [i for i, r in enumerate(history) if _weekend(r)]
    pattern = {
        "pattern_status": "not_applicable",
        "pattern_target_type": "weekend" if (cutoff_local + timedelta(days=1)).weekday() >= 5 else "weekday",
        "pattern_weekday_count": len(weekdays),
        "pattern_weekend_count": len(weekends),
        "pattern_type_mass": 0.0,
        "pattern_weight": 0.0,
        "pattern_bed_type_shift_min": 0.0,
        "pattern_wake_type_shift_min": 0.0,
        "pattern_bed_applied_shift_min": 0.0,
        "pattern_wake_applied_shift_min": 0.0,
    }
    if spec["pattern"]:
        enough = (mass > 0.0 and len(weekdays) >= spec["minimum_each_weektype"]
                  and len(weekends) >= spec["minimum_each_weektype"])
        pattern["pattern_status"] = "exploratory" if enough else "insufficient"
        if enough:
            target_weekend = pattern["pattern_target_type"] == "weekend"
            indices = weekends if target_weekend else weekdays
            type_weights = [weights[i] for i in indices]
            type_mass = float(sum(type_weights))
            type_bed = _weighted_median([history[i]["bed_offset"] for i in indices], type_weights)
            type_wake = _weighted_median([history[i]["wake_offset"] for i in indices], type_weights)
            pattern_weight = min(spec["pattern_weight_cap"], type_mass / (type_mass + spec["pattern_lambda"]))
            bed_shift = type_bed - bed_center
            wake_shift = type_wake - wake_center
            applied_bed = pattern_weight * bed_shift
            applied_wake = pattern_weight * wake_shift
            habit_bed += applied_bed
            habit_wake += applied_wake
            pattern.update(pattern_type_mass=type_mass, pattern_weight=pattern_weight,
                           pattern_bed_type_shift_min=bed_shift,
                           pattern_wake_type_shift_min=wake_shift,
                           pattern_bed_applied_shift_min=applied_bed,
                           pattern_wake_applied_shift_min=applied_wake)

    habit_bed, habit_wake = constrain(habit_bed, habit_wake)
    phase, high, low = stationary_thresholds(habit_bed, habit_wake)
    current_s = reconstruct_s(history, cutoff_local, habit_bed, habit_wake)
    physical_bed, physical_wake, fallback = physical_prediction(
        current_s, cutoff_local, phase, high, low, (habit_bed, habit_wake))

    values.update(
        habit_bed=habit_bed,
        habit_wake=habit_wake,
        personal_weight=personal_weight,
        current_s=current_s,
        current_c=circadian(cutoff_local, phase),
        sleep_threshold=high,
        wake_threshold=low,
        physical_bed=physical_bed,
        physical_wake=physical_wake,
    )
    state.update(
        valid_days=len(history),
        personal_weight=personal_weight,
        mode="personalized_daily" if mass > 0.0 else "population",
        habit_bed_offset=habit_bed,
        habit_wake_offset=habit_wake,
        phase_min=phase,
        sleep_threshold=high,
        wake_threshold=low,
        physical_fallback=fallback,
        personalization_policy=policy_name,
        decayed_mass=mass,
        effective_n=effective_n,
        last_gap_days=((cutoff_local - history[-1]["wake"]).total_seconds() / 86400.0
                       if history else None),
        activation_rule="first eligible normalized record",
        physiological_tau_estimated=False,
        **pattern,
    )
    return values, state

