"""Leakage-safe online residual personalization ablations.

The base forecasts are frozen.  At each cutoff, a correction may use only
earlier forecasts whose outcomes were both completed and recorded strictly
before that cutoff.  Residuals always refer to the original frozen forecast,
never to a previously corrected forecast.

For retrospective cohorts, ``observed_at`` is the recorded or reconstructed
availability time.  Correct replay prevents mechanical future leakage but does
not independently prove that reconstructed availability matches prospective
collection.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Literal

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Policy:
    minimum_history: int
    half_life_days: float
    shrinkage_lambda: float
    aggregate: Literal["mean", "weighted_median"]
    correction_cap_min: float
    residual_clip_min: float
    context_weekend_multiplier: float = 1.0


POLICIES = {
    "none": Policy(1, 14.0, 14.0, "mean", 0.0, 180.0),
    # Fixed minimum-14 comparator.  The deployed v1 sidecar has the same
    # history/decay/shrinkage constants but no explicit +/-90 min final cap,
    # so this is deliberately not labelled as the legacy policy.
    "fixed14": Policy(14, 14.0, 14.0, "mean", 90.0, math.inf),
    "mean3": Policy(3, 14.0, 14.0, "mean", 60.0, 180.0),
    "median3": Policy(3, 14.0, 7.0, "weighted_median", 60.0, 180.0),
    "median7": Policy(7, 14.0, 14.0, "weighted_median", 60.0, 180.0),
    "mean3_long": Policy(3, 28.0, 28.0, "mean", 60.0, 180.0),
    "context3": Policy(3, 14.0, 14.0, "mean", 60.0, 180.0, 2.0),
}

REQUIRED_COLUMNS = (
    "row_id", "subject_id", "split", "cutoff", "observed_at", "wake",
    "bed_offset", "wake_offset",
)


def policy_specs() -> dict[str, dict]:
    """Return JSON-safe, immutable-value policy specifications for manifests."""
    result = {}
    for name, policy in POLICIES.items():
        spec = asdict(policy)
        if not math.isfinite(spec["residual_clip_min"]):
            spec["residual_clip_min"] = "unbounded"
        result[name] = spec
    return result


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    cumulative = np.cumsum(weights[order])
    return float(sorted_values[np.searchsorted(cumulative, weights.sum() / 2.0, side="left")])


def _project(values: np.ndarray) -> np.ndarray:
    result = np.empty_like(values, dtype=float)
    for index, (bed, wake) in enumerate(np.asarray(values, dtype=float)):
        bed = float(np.clip(bed, 0.0, 960.0))
        wake = float(np.clip(wake, bed + 30.0, min(bed + 960.0, 1440.0)))
        result[index] = (bed, wake)
    return result


def _validate(frame: pd.DataFrame, predictions: np.ndarray) -> tuple[pd.DataFrame, np.ndarray]:
    missing = [name for name in REQUIRED_COLUMNS if name not in frame]
    if missing:
        raise ValueError(f"missing required columns: {missing}")
    if frame.row_id.isna().any() or frame.row_id.duplicated().any():
        raise ValueError("row_id must be present and unique")
    if frame.subject_id.isna().any() or frame.split.isna().any():
        raise ValueError("subject_id and split must be present")
    predictions = np.asarray(predictions, dtype=float)
    if predictions.shape != (len(frame), 2) or not np.isfinite(predictions).all():
        raise ValueError("predictions must be a finite n-by-2 array")
    actual = frame[["bed_offset", "wake_offset"]].to_numpy(float)
    if not np.isfinite(actual).all():
        raise ValueError("targets must be finite")
    parsed = frame.copy()
    for name in ("cutoff", "observed_at", "wake"):
        parsed[name] = pd.to_datetime(parsed[name], utc=True, errors="coerce")
        if parsed[name].isna().any():
            raise ValueError(f"{name} must contain finite timestamps")
    if (parsed.observed_at < parsed.wake).any():
        raise ValueError("observed_at must not precede the wake outcome")
    return parsed, predictions


def replay_personalization(
    frame: pd.DataFrame,
    predictions: np.ndarray,
    policy_name: str,
) -> tuple[np.ndarray, pd.DataFrame]:
    """Replay a frozen residual-correction policy in causal cutoff order.

    Output rows follow the caller's original row order.  History is isolated by
    both participant and split, which also makes reused Train/Validation/Test
    partitions independent personalization episodes.
    """
    if policy_name not in POLICIES:
        raise ValueError(f"unknown policy: {policy_name}")
    policy = POLICIES[policy_name]
    parsed, predictions = _validate(frame, predictions)
    base = _project(predictions)
    corrected = np.empty_like(base)
    trace: list[dict | None] = [None] * len(parsed)
    staged = parsed.assign(position=np.arange(len(parsed), dtype=int))

    for _, group in staged.groupby(["subject_id", "split"], sort=False, dropna=False):
        history: list[dict] = []
        ordered = group.sort_values(["cutoff", "row_id"], kind="mergesort")
        for row in ordered.itertuples(index=False):
            position = int(row.position)
            cutoff = row.cutoff
            eligible = [
                item for item in history
                if item["observed_at"] < cutoff and item["wake"] < cutoff
            ]
            n_prior = len(eligible)
            mass = 0.0
            kish = 0.0
            alpha = 0.0
            gap_days = math.nan
            correction = np.zeros(2, dtype=float)
            active = policy_name != "none" and n_prior >= policy.minimum_history
            if eligible:
                ages = np.array([(cutoff - item["wake"]).total_seconds() / 86400.0 for item in eligible])
                if not np.isfinite(ages).all() or (ages <= 0).any():
                    raise ValueError("eligible outcome ages must be positive and finite")
                weights = np.power(2.0, -ages / policy.half_life_days)
                # Research cutoffs are normalized fixed 20:00 local cutoffs;
                # their UTC date plus one day has the same target weekday.
                current_weekend = (cutoff + pd.Timedelta(days=1)).weekday() >= 5
                if policy.context_weekend_multiplier != 1.0:
                    weights *= np.array([
                        policy.context_weekend_multiplier
                        if item["target_weekend"] == current_weekend else 1.0
                        for item in eligible
                    ])
                mass = float(weights.sum())
                sum_squares = float(np.square(weights).sum())
                kish = mass * mass / sum_squares if sum_squares else 0.0
                gap_days = float(ages.min())
                if active and mass > 0:
                    residuals = np.vstack([item["residual"] for item in eligible])
                    residuals = np.clip(residuals, -policy.residual_clip_min, policy.residual_clip_min)
                    if policy.aggregate == "mean":
                        aggregate = np.average(residuals, axis=0, weights=weights)
                    else:
                        aggregate = np.array([
                            _weighted_median(residuals[:, target], weights) for target in range(2)
                        ])
                    alpha = mass / (mass + policy.shrinkage_lambda)
                    correction = np.clip(
                        alpha * aggregate,
                        -policy.correction_cap_min,
                        policy.correction_cap_min,
                    )
            corrected[position] = _project(base[position:position + 1] + correction)[0]
            trace[position] = {
                "row_id": row.row_id,
                "n_prior": n_prior,
                "mass": mass,
                "kish_n": kish,
                "alpha": alpha,
                "gap_days": gap_days,
                "active": active,
                "onset_correction": float(correction[0]),
                "wake_correction": float(correction[1]),
                "history_max_available": len(history),
                "policy": policy_name,
            }
            actual = np.array([float(row.bed_offset), float(row.wake_offset)])
            history.append({
                "row_id": row.row_id,
                "observed_at": row.observed_at,
                "wake": row.wake,
                "residual": actual - base[position],
                "target_weekend": (row.cutoff + pd.Timedelta(days=1)).weekday() >= 5,
            })

    return corrected, pd.DataFrame(trace)

