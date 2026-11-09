"""Causal robust history summaries for a research-only next-night comparison."""
from __future__ import annotations

import math
import sys
from datetime import timedelta
from pathlib import Path
from statistics import median, mean

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "web-app"))
from night_core import features, normalize_history, DATA_FEATURES, PHYSICAL_FEATURES

EXTRA = (
    "next_day_sin", "next_day_cos", "next_day_weekend",
    "median14_bed", "median14_wake", "ew_bed", "ew_wake",
    "weekday_bed", "weekday_wake", "weekday_count",
    "bed_mad", "wake_mad", "duration_mad", "previous_duration",
    "bed_trend", "wake_trend", "gap_days_mean", "recent_count14",
)
DATA_V2 = DATA_FEATURES + EXTRA
HYBRID_V2 = DATA_V2 + PHYSICAL_FEATURES


def weighted_median(values, weights):
    pairs = sorted(zip(values, weights))
    threshold = sum(weights) / 2
    cumulative = 0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= threshold:
            return value
    raise ValueError("Weighted median needs observations")


def extended_features(records, cutoff, prior, enrolled_at):
    output, state = features(records, cutoff, prior, enrolled_at=enrolled_at)
    history = normalize_history(records, cutoff, enrolled_at)
    recent = [r for r in history if r["wake"] >= cutoff - timedelta(days=56)][-14:]
    next_day = cutoff + timedelta(days=1)
    output.update(next_day_sin=math.sin(math.tau * next_day.weekday() / 7),
                  next_day_cos=math.cos(math.tau * next_day.weekday() / 7),
                  next_day_weekend=float(next_day.weekday() >= 5), recent_count14=float(len(recent)))
    same_weektype = [r for r in recent if (r["wake"].weekday() >= 5) == (next_day.weekday() >= 5)]
    output["weekday_count"] = float(len(same_weektype))
    for name, field, initial in [("bed", "bed_offset", prior["bed_offset"]),
                                 ("wake", "wake_offset", prior["wake_offset"])]:
        values = [r[field] for r in recent]
        center = median(values) if values else initial
        output[f"median14_{name}"] = center
        weights = [math.exp(-math.log(2) * (cutoff - r["wake"]).total_seconds() / 86400 / 7) for r in recent]
        output[f"ew_{name}"] = weighted_median(values, weights) if values else initial
        matched = [r[field] for r in same_weektype]
        # Small categories shrink to the broader recent median.
        w = len(matched) / (len(matched) + 4)
        output[f"weekday_{name}"] = w * median(matched) + (1-w) * center if matched else center
        output[f"{name}_mad"] = median([abs(v-center) for v in values]) if values else 0.
        output[f"{name}_trend"] = median(values[-3:]) - median(values[:-3]) if len(values) >= 6 else 0.
    durations = [r["duration"] for r in recent]
    output["duration_mad"] = median([abs(v-median(durations)) for v in durations]) if durations else 0.
    output["previous_duration"] = durations[-1] if durations else prior["wake_offset"] - prior["bed_offset"]
    gaps = [(b["wake"] - a["wake"]).total_seconds() / 86400 for a, b in zip(recent, recent[1:])]
    output["gap_days_mean"] = mean(gaps) if gaps else 0.
    return output, state
