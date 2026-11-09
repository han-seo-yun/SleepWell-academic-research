"""Timing policy for recommendation previews and awake-only hourly revisions.

The frozen research-model cutoff remains 20:00 Asia/Seoul.  This module only
schedules when a recommendation may be shown or revised; it never shifts model
features or target offsets to a different cutoff.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Seoul")
INITIAL_LEAD = timedelta(hours=2)
UPDATE_HOURS = (20, 21, 22, 23)
POLICY_VERSION = "recommendation-timing-v1"


def _local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=ZONE)
    return value.astimezone(ZONE)


def schedule(night_id: str | date, expected_onset: datetime) -> dict:
    night = date.fromisoformat(night_id) if isinstance(night_id, str) else night_id
    cutoff = datetime.combine(night, time(20, 0), ZONE)
    onset = _local(expected_onset)
    initial = min(cutoff, onset - INITIAL_LEAD)
    return {
        "night_id": night.isoformat(),
        "model_cutoff": cutoff,
        "expected_onset": onset,
        "initial_decision_at": initial,
        "hourly_updates": tuple(cutoff.replace(hour=hour) for hour in UPDATE_HOURS),
    }


def due_slot(now: datetime, timing: dict, seen_slots=(), *, awake: bool) -> dict | None:
    """Return one non-backfilled slot due during the current interaction.

    `awake` must be direct interaction or an explicit current-state observation;
    absence of a sleep record is not evidence that the user is awake.
    """
    current = _local(now)
    seen = set(seen_slots)
    cutoff = _local(timing["model_cutoff"])
    initial = _local(timing["initial_decision_at"])
    if not awake:
        return None
    if initial < cutoff and initial <= current < cutoff and "initial" not in seen:
        return {"slot": "initial", "scheduled_at": initial, "kind": "personalized_initial"}
    if current.date() != cutoff.date() or current.hour not in UPDATE_HOURS:
        return None
    scheduled = cutoff.replace(hour=current.hour)
    if not scheduled <= current < scheduled + timedelta(hours=1):
        return None
    initial_collision = initial == cutoff and current.hour == 20
    slot = "initial" if initial_collision else f"hourly_{current.hour:02d}"
    if slot in seen:
        return None
    kind = "personalized_initial" if initial_collision else "conditional_hourly"
    return {"slot": slot, "scheduled_at": scheduled, "kind": kind}


def public_schedule(timing: dict) -> dict:
    return {
        "night_id": timing["night_id"],
        "model_cutoff": timing["model_cutoff"].isoformat(),
        "expected_onset": timing["expected_onset"].isoformat(),
        "initial_decision_at": timing["initial_decision_at"].isoformat(),
        "hourly_updates": [value.isoformat() for value in timing["hourly_updates"]],
        "policy": "min_20_or_expected_onset_minus_2h_then_awake_hourly_20_23_v1",
        "research_cutoff_unchanged": True,
    }
