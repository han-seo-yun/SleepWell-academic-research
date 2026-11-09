"""Causal online residual personalization for frozen next-night forecasts.

This sidecar corrects a frozen model only after outcomes from earlier nights
have become available.  It does not estimate physiological time constants and
does not replace the original research-model output.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


POLICY_VERSION = "online-residual-shrinkage-v2-daily"
UNCERTAINTY_REASON = "검증된 예측구간이 없어 제공하지 않음"
# Prespecified engineering bounds, not literature-derived clinical safety limits.
# The correction cap is relative to the frozen forecast, not yesterday's output.
# Derivation, scope, and comparison in the canonical document (workspace-relative):
# work/dayone-personalization-20261027/docs/PARAMETER_EVIDENCE_2026-11-04.md
RESIDUAL_CLIP_MIN = 180.0
CORRECTION_CAP_MIN = 60.0


def _time(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


# Geometric recency and zero-centered shrinkage have statistical precedents:
# Roberts (1959), doi:10.1080/00401706.1959.10489860;
# Hoerl & Kennard (1970), doi:10.1080/00401706.1970.10488634.
# These support the principles, NOT the specific defaults 14, 14, or 1 below.
def compute_adjustment(
    history_residuals,
    cutoff,
    *,
    half_life_days=14.0,
    shrinkage_lambda=14.0,
    minimum_history=1,
):
    """Return a pre-specified recency-weighted correction using past outcomes.

    Residuals use ``actual - frozen_prediction``.  A row is eligible only when
    its outcome was available strictly before ``cutoff``.  If ``night_id`` is
    present, the latest eligible revision for that night is used.
    """
    cutoff_time = _time(cutoff)
    half_life_days = _finite(half_life_days, "half_life_days")
    shrinkage_lambda = _finite(shrinkage_lambda, "shrinkage_lambda")
    if half_life_days <= 0 or shrinkage_lambda <= 0:
        raise ValueError("half-life and shrinkage lambda must be positive")
    if isinstance(minimum_history, bool) or not isinstance(minimum_history, int) or minimum_history < 1:
        raise ValueError("minimum_history must be a positive integer")

    eligible = []
    for index, row in enumerate(history_residuals):
        try:
            available = _time(row["available_at"])
            outcome = _time(row.get("outcome_at", row["available_at"]))
            onset = _finite(row["onset_residual_min"], "onset_residual_min")
            wake = _finite(row["wake_residual_min"], "wake_residual_min")
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if available >= cutoff_time or outcome > available:
            continue
        night_id = str(row.get("night_id", f"__row_{index}"))
        eligible.append((night_id, available, outcome, onset, wake))

    latest = {}
    for row in eligible:
        prior = latest.get(row[0])
        if prior is None or row[1] > prior[1]:
            latest[row[0]] = row
    rows = sorted(latest.values(), key=lambda row: (row[1], row[0]))
    outcome_ages = [(cutoff_time - row[2]).total_seconds() / 86400.0 for row in rows]
    available_ages = [(cutoff_time - row[1]).total_seconds() / 86400.0 for row in rows]
    ages = outcome_ages
    weights = [2.0 ** (-age / half_life_days) for age in ages]
    mass = sum(weights)
    sum_squares = sum(weight * weight for weight in weights)
    kish = mass * mass / sum_squares if sum_squares else 0.0
    clipped = [(min(RESIDUAL_CLIP_MIN, max(-RESIDUAL_CLIP_MIN, row[3])),
                min(RESIDUAL_CLIP_MIN, max(-RESIDUAL_CLIP_MIN, row[4]))) for row in rows]
    raw_onset = sum(weight * value[0] for weight, value in zip(weights, clipped)) / mass if mass else 0.0
    raw_wake = sum(weight * value[1] for weight, value in zip(weights, clipped)) / mass if mass else 0.0
    active = len(rows) >= minimum_history and mass > 0
    shrinkage = mass / (mass + shrinkage_lambda) if active else 0.0
    onset_correction = min(CORRECTION_CAP_MIN, max(-CORRECTION_CAP_MIN, shrinkage * raw_onset))
    wake_correction = min(CORRECTION_CAP_MIN, max(-CORRECTION_CAP_MIN, shrinkage * raw_wake))
    return {
        "policy_version": POLICY_VERSION,
        "eligible_count": len(rows),
        "decayed_mass": mass,
        "kish_effective_n": kish,
        "weight": shrinkage,
        "raw_onset_residual_mean_min": raw_onset if rows else None,
        "raw_wake_residual_mean_min": raw_wake if rows else None,
        "onset_correction_min": onset_correction,
        "wake_correction_min": wake_correction,
        "last_outcome_gap_days": min(outcome_ages) if outcome_ages else None,
        "last_available_gap_days": min(available_ages) if available_ages else None,
        "active": active,
        "minimum_history": minimum_history,
        "half_life_days": half_life_days,
        "shrinkage_lambda": shrinkage_lambda,
        "residual_clip_min": RESIDUAL_CLIP_MIN,
        "correction_cap_min": CORRECTION_CAP_MIN,
        "uncertainty_min": None,
        "uncertainty_reason": UNCERTAINTY_REASON,
        "interpretation": "첫 과거 확정 결과부터 매일 갱신하는 온라인 보정이며 개인 생리상수 추정값이 아님",
    }
