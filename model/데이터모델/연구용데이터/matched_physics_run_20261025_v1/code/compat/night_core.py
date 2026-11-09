"""Shared, causal next-night features and a two-process threshold baseline.

Clock offsets are minutes after a fixed local 20:00 decision. The model predicts
main-sleep onset and final wake, not PSG stages, total sleep time, or benefit.
Only finished, available records enter features. Population and model fitting
belong to training code; this module uses only the Python standard library.
"""
from __future__ import annotations

import math
import json
from datetime import datetime, timedelta, timezone
from statistics import mean, median, pstdev

CONTRACT = "sleepwell_next_night_pre20_v1"
ZONE = timezone(timedelta(hours=9))
TAU_WAKE, TAU_SLEEP, C_AMPLITUDE = 18.2, 4.2, 0.15
DATA_FEATURES = (
    "population_bed", "population_wake", "habit_bed", "habit_wake",
    "recent_bed", "recent_wake", "previous_bed", "previous_wake",
    "recent_duration", "bed_variability", "wake_variability",
    "history_count_7", "history_age_days", "personal_weight", "weekend",
)
PHYSICAL_FEATURES = (
    "current_s", "current_c", "sleep_threshold", "wake_threshold",
    "physical_bed", "physical_wake",
)


def local_time(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return result.replace(tzinfo=ZONE) if result.tzinfo is None else result.astimezone(ZONE)


def minute(value):
    return value.hour * 60 + value.minute + value.second / 60


def cutoff_for_onset(onset):
    onset = local_time(onset)
    cutoff = onset.replace(hour=20, minute=0, second=0, microsecond=0)
    return cutoff if onset >= cutoff else cutoff - timedelta(days=1)


def normalize_history(records, cutoff, enrolled_at=None):
    """Latest available edit per wake date; archive/unknown availability excluded."""
    cutoff = local_time(cutoff)
    enrollment = local_time(enrolled_at) if enrolled_at is not None else None
    by_date = {}
    for record in records:
        if record.get("source") not in ("diary", "healthkit", "health_connect", "research"):
            continue
        try:
            bed = local_time(record.get("sleep_start") or record["bed"])
            if not record.get("sleep_start"):
                bed += timedelta(minutes=float(record.get("latency_min") or 0))
            wake = local_time(record["wake"])
            observed = local_time(record["observed_at"])
            duration = (wake - bed).total_seconds() / 60
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if not (bed < wake < cutoff and observed < cutoff and 30 <= duration <= 960):
            continue
        if enrollment is not None and wake < enrollment:
            continue
        bed_offset = (minute(bed) - 1200) % 1440
        wake_offset = bed_offset + duration
        if bed_offset > 960 or wake_offset > 1440:
            continue
        row = dict(bed=bed, wake=wake, observed=observed, bed_offset=bed_offset,
                   wake_offset=wake_offset, duration=duration,
                   tie_key=json.dumps(record, sort_keys=True, ensure_ascii=False))
        key = wake.date()
        if key not in by_date or (observed, row["tie_key"]) > (by_date[key]["observed"], by_date[key]["tie_key"]):
            by_date[key] = row
    return sorted(by_date.values(), key=lambda r: r["wake"])


def advance(s, asleep, hours):
    if hours < 0:
        raise ValueError("State cannot advance backwards")
    return s * math.exp(-hours / TAU_SLEEP) if asleep else 1 - (1 - s) * math.exp(-hours / TAU_WAKE)


def circadian(t, phase):
    return C_AMPLITUDE * math.cos(math.tau * (minute(t) - phase) / 1440)


def stationary_thresholds(bed_offset, wake_offset):
    """Periodic reference schedule maps S to two observable transition thresholds.

    This is an explicit engineering observation model, not measured physiology.
    Its time constants stay fixed; prior/history shifts the reference schedule.
    """
    duration = min(960.0, max(30.0, wake_offset - bed_offset))
    asleep_factor = math.exp(-duration / 60 / TAU_SLEEP)
    awake_factor = math.exp(-(24 - duration / 60) / TAU_WAKE)
    s_wake = asleep_factor * (1 - awake_factor) / (1 - asleep_factor * awake_factor)
    s_bed = 1 - (1 - s_wake) * awake_factor
    phase = (1200 + (bed_offset + wake_offset) / 2) % 1440
    c_edge = C_AMPLITUDE * math.cos(math.tau * duration / 2 / 1440)
    return phase, s_bed + c_edge, s_wake + c_edge


def reconstruct_s(history, cutoff, bed_offset, wake_offset):
    """Reconstruct seven past days; missing nights use the reference schedule."""
    start = cutoff - timedelta(days=7)
    actual = [(r["bed"], r["wake"]) for r in history if r["wake"] > start]
    known_cutoffs = {cutoff_for_onset(r["bed"]).date() for r in history}
    intervals = list(actual)
    for day in range(-8, 0):
        anchor = cutoff + timedelta(days=day)
        if anchor.date() not in known_cutoffs:
            intervals.append((anchor + timedelta(minutes=bed_offset),
                              anchor + timedelta(minutes=wake_offset)))
    edges = {start, cutoff}
    for bed, wake in intervals:
        if start < bed < cutoff:
            edges.add(bed)
        if start < wake < cutoff:
            edges.add(wake)
    ordered = sorted(edges)
    s = 0.5
    for left, right in zip(ordered, ordered[1:]):
        asleep = any(b <= left < w for b, w in intervals)
        s = advance(s, asleep, (right - left).total_seconds() / 3600)
    return s


def physical_prediction(s, cutoff, phase, high, low, fallback):
    """Simulate hysteretic sleep/wake transitions on a fixed five-minute grid."""
    onset = None
    wake = None
    for offset in range(0, 1441, 5):
        t = cutoff + timedelta(minutes=offset)
        if offset:
            s = advance(s, onset is not None, 5 / 60)
        drive = s + circadian(t, phase)
        if onset is None and offset <= 960 and drive >= high:
            onset = float(offset)
        elif onset is not None and offset >= onset + 30 and drive <= low:
            wake = float(offset)
            break
    if onset is None or wake is None or not 30 <= wake - onset <= 960:
        return float(fallback[0]), float(fallback[1]), True
    return onset, wake, False


def features(records, cutoff, prior, personalize=True, enrolled_at=None):
    cutoff = local_time(cutoff)
    if (cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond) != (20, 0, 0, 0):
        raise ValueError("Forecast decision must be exactly local 20:00")
    pb, pw = float(prior["bed_offset"]), float(prior["wake_offset"])
    if not (math.isfinite(pb) and math.isfinite(pw) and 0 <= pb <= 960 and pb + 30 <= pw <= 1440 and pw - pb <= 960):
        raise ValueError("Invalid population night prior")
    history = normalize_history(records, cutoff, enrolled_at)
    n = len(history)
    start = local_time(enrolled_at) if enrolled_at else (history[0]["wake"] if history else cutoff)
    elapsed = max(0, (cutoff.date() - start.date()).days)
    active = personalize and n >= 14 and elapsed >= 14
    weight = n / (n + 14) if active else 0.0
    hb = (1 - weight) * pb + weight * mean([r["bed_offset"] for r in history]) if n else pb
    hw = (1 - weight) * pw + weight * mean([r["wake_offset"] for r in history]) if n else pw
    recent = [r for r in history if r["wake"] >= cutoff - timedelta(days=28)][-7:]
    beds = [r["bed_offset"] for r in recent]
    wakes = [r["wake_offset"] for r in recent]
    phase, high, low = stationary_thresholds(hb, hw)
    s = reconstruct_s(history, cutoff, hb, hw)
    phys_bed, phys_wake, fallback = physical_prediction(s, cutoff, phase, high, low, (hb, hw))
    values = {
        "population_bed": pb, "population_wake": pw, "habit_bed": hb, "habit_wake": hw,
        "recent_bed": median(beds) if beds else pb, "recent_wake": median(wakes) if wakes else pw,
        "previous_bed": beds[-1] if beds else pb, "previous_wake": wakes[-1] if wakes else pw,
        "recent_duration": median([r["duration"] for r in recent]) if recent else pw - pb,
        "bed_variability": pstdev(beds) if len(beds) > 1 else 0.,
        "wake_variability": pstdev(wakes) if len(wakes) > 1 else 0.,
        "history_count_7": float(len(recent)),
        "history_age_days": min(28., (cutoff - recent[-1]["wake"]).total_seconds() / 86400) if recent else 28.,
        "personal_weight": weight, "weekend": float(cutoff.weekday() >= 5),
        "current_s": s, "current_c": circadian(cutoff, phase),
        "sleep_threshold": high, "wake_threshold": low,
        "physical_bed": phys_bed, "physical_wake": phys_wake,
    }
    state = {
        "valid_days": n, "elapsed_days": elapsed, "personal_weight": weight,
        "mode": "personalized" if active else "population",
        "habit_bed_offset": hb, "habit_wake_offset": hw, "phase_min": phase,
        "tau_wake_h": TAU_WAKE, "tau_sleep_h": TAU_SLEEP,
        "sleep_threshold": high, "wake_threshold": low,
        "physical_fallback": fallback,
        "note": "행동 기록으로 보정한 위상·전환 기준. 생리 시간상수는 고정이며 측정된 개인 생리값이 아닙니다.",
    }
    return values, state


def constrain(bed, wake):
    """Identical, declared support projection for every regression candidate."""
    bed = min(960., max(0., float(bed)))
    wake = min(1440., max(bed + 30., min(bed + 960., float(wake))))
    return bed, wake
