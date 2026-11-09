"""Descriptive statistics of the observed sleep rows in a research-lab run.

Two summaries borrowed from established open-source sleep tooling, computed only from
the participant's own observed main-sleep intervals (never from model predictions):

* regularity - Sleep Regularity Index (Phillips et al. 2017; sleepreg / GGIR), interdaily
  stability and intradaily variability (Witting et al. 1990; pyActigraphy / GGIR) on a
  binary sleep series, and timing variability.
* completeness - per-night data availability and reporting delay, in the spirit of the
  wear/compliance dashboards of Beiwe, RADAR-base and ActiGraph CentrePoint. It shows
  whether each observed night was available before the next 20:00 decision.

Both are descriptions of the supplied data. They are not model accuracy, not clinical
measures, and they inherit every limitation of the rows (main sleep only, naps unknown,
timestamps not authenticated).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import math
from statistics import mean, median, pstdev

import numpy as np

MINUTES_PER_DAY = 1440
MIN_SRI_DAY_PAIRS = 7
MIN_RHYTHM_DAYS = 7
MIN_WEEKPART_NIGHTS = 2
WEEKEND_CUTOFF_WEEKDAYS = frozenset({4, 5})  # Friday and Saturday 20:00 cutoffs

ASSUMPTIONS = {
    "window": "Each labeled row covers [cutoff, cutoff + 24 h); minutes inside the observed main sleep are sleep, all other minutes are treated as wake.",
    "naps": "Naps and secondary sleep are not part of the canonical rows, so daytime sleep is invisible to these indices.",
    "unlabeled": "Prediction-only and missing nights are unknown, never wake; pairs touching them are excluded.",
    "sri": f"Sleep Regularity Index on 1-minute epochs over consecutive labeled day pairs (-100..100); reported when at least {MIN_SRI_DAY_PAIRS} pairs exist.",
    "is_iv": f"Interdaily stability and intradaily variability on hourly sleep fractions of labeled days; reported with at least {MIN_RHYTHM_DAYS} labeled days. Binary-sleep values are not comparable with activity-count IS/IV.",
    "weekend": "Weekend nights are the Friday and Saturday 20:00 cutoffs; work schedules are not known, so the weekend-weekday midpoint gap only approximates social jetlag.",
    "timely": "A labeled night is timely when observed_at is before the next 20:00 cutoff, i.e. it could enter the next night's prediction history.",
}


def _clock(offset_min):
    """Minutes after the 20:00 cutoff -> HH:MM clock label."""
    if offset_min is None:
        return None
    total = int(round(20 * 60 + offset_min)) % MINUTES_PER_DAY
    return f"{total // 60:02d}:{total % 60:02d}"


def _round(value, digits=2):
    return None if value is None else round(float(value), digits)


def _sd(values):
    return pstdev(values) if len(values) >= 2 else None


def _offsets(row):
    onset = (row["sleep_start"] - row["cutoff"]).total_seconds() / 60.0
    wake = (row["sleep_end"] - row["cutoff"]).total_seconds() / 60.0
    return onset, wake


def _day_state(row):
    onset, wake = _offsets(row)
    minutes = np.arange(MINUTES_PER_DAY)
    return ((minutes >= math.floor(onset)) & (minutes < math.ceil(wake))).astype(np.int8)


def _participant_regularity(rows):
    labeled = [row for row in rows if row["sleep_start"] is not None]
    by_cutoff = {row["cutoff"]: row for row in labeled}
    states = {row["cutoff"]: _day_state(row) for row in labeled}

    agreements = []
    for cutoff in sorted(states):
        following = cutoff + timedelta(days=1)
        if following in states:
            agreements.append(float(np.mean(states[cutoff] == states[following])))
    sri = None
    if len(agreements) >= MIN_SRI_DAY_PAIRS:
        sri = -100.0 + 200.0 * mean(agreements)

    interdaily = intradaily = None
    if len(labeled) >= MIN_RHYTHM_DAYS:
        hourly = {cutoff: state.reshape(24, 60).mean(axis=1) for cutoff, state in states.items()}
        matrix = np.vstack([hourly[cutoff] for cutoff in sorted(hourly)])
        overall = matrix.mean()
        variance = float(np.mean((matrix - overall) ** 2))
        if variance > 0.0:
            interdaily = float(np.mean((matrix.mean(axis=0) - overall) ** 2)) / variance
            differences = []
            for cutoff in sorted(hourly):
                series = hourly[cutoff]
                differences.extend(np.diff(series).tolist())
                following = cutoff + timedelta(days=1)
                if following in hourly:
                    differences.append(float(hourly[following][0] - series[-1]))
            if differences:
                intradaily = float(np.mean(np.square(differences))) / variance

    onsets, wakes, midpoints, durations = [], [], [], []
    weekend_mid, weekday_mid = [], []
    for row in labeled:
        onset, wake = _offsets(row)
        midpoint = (onset + wake) / 2.0
        onsets.append(onset)
        wakes.append(wake)
        midpoints.append(midpoint)
        durations.append(wake - onset)
        (weekend_mid if row["cutoff"].weekday() in WEEKEND_CUTOFF_WEEKDAYS else weekday_mid).append(midpoint)
    weekend_gap = None
    if len(weekend_mid) >= MIN_WEEKPART_NIGHTS and len(weekday_mid) >= MIN_WEEKPART_NIGHTS:
        weekend_gap = mean(weekend_mid) - mean(weekday_mid)

    reasons = []
    if sri is None:
        reasons.append(f"SRI: 연속된 관측 밤 쌍 {len(agreements)}개 (최소 {MIN_SRI_DAY_PAIRS}개)")
    if interdaily is None:
        reasons.append(f"IS/IV: 관측 밤 {len(labeled)}개 (최소 {MIN_RHYTHM_DAYS}개)")
    if weekend_gap is None:
        reasons.append(f"주말-주중: 주말 {len(weekend_mid)}밤·주중 {len(weekday_mid)}밤 (각 최소 {MIN_WEEKPART_NIGHTS}밤)")

    return {
        "labeled_nights": len(labeled),
        "sri_day_pairs": len(agreements),
        "sri": _round(sri, 1),
        "interdaily_stability": _round(interdaily, 3),
        "intradaily_variability": _round(intradaily, 3),
        "onset_mean_clock": _clock(mean(onsets)) if onsets else None,
        "wake_mean_clock": _clock(mean(wakes)) if wakes else None,
        "midpoint_mean_clock": _clock(mean(midpoints)) if midpoints else None,
        "onset_sd_min": _round(_sd(onsets), 1),
        "wake_sd_min": _round(_sd(wakes), 1),
        "midpoint_sd_min": _round(_sd(midpoints), 1),
        "duration_mean_min": _round(mean(durations), 1) if durations else None,
        "duration_sd_min": _round(_sd(durations), 1),
        "weekend_weekday_midpoint_gap_min": _round(weekend_gap, 1),
        "weekend_nights": len(weekend_mid),
        "weekday_nights": len(weekday_mid),
        "unavailable": reasons,
    }


def _percentile(values, fraction):
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def _participant_completeness(rows):
    by_date = {row["cutoff"].date(): row for row in rows}
    first, last = min(by_date), max(by_date)
    span_days = (last - first).days + 1
    grid = []
    delays = []
    counts = defaultdict(int)
    for index in range(span_days):
        day = first + timedelta(days=index)
        row = by_date.get(day)
        if row is None:
            status, delay = "missing", None
        elif row["sleep_start"] is None:
            status, delay = "prediction_only", None
        else:
            delay = (row["observed_at"] - row["sleep_end"]).total_seconds() / 60.0
            delays.append(delay)
            next_cutoff = row["cutoff"] + timedelta(days=1)
            status = "timely" if row["observed_at"] < next_cutoff else "late"
        counts[status] += 1
        grid.append({"date": day.isoformat(), "status": status,
                     "report_delay_min": _round(delay, 1)})
    labeled = counts["timely"] + counts["late"]
    return {
        "first_date": first.isoformat(),
        "last_date": last.isoformat(),
        "span_days": span_days,
        "rows": len(rows),
        "labeled_nights": labeled,
        "timely_nights": counts["timely"],
        "late_nights": counts["late"],
        "prediction_only_nights": counts["prediction_only"],
        "missing_nights": counts["missing"],
        "labeled_coverage": _round(labeled / span_days, 3),
        "timely_share_of_labeled": _round(counts["timely"] / labeled, 3) if labeled else None,
        "report_delay_median_min": _round(median(delays), 1) if delays else None,
        "report_delay_p90_min": _round(_percentile(delays, 0.9), 1) if delays else None,
        "report_delay_max_min": _round(max(delays), 1) if delays else None,
        "grid": grid,
    }


def describe_observations(rows):
    """Regularity and completeness per participant from normalized lab rows."""
    by_participant = defaultdict(list)
    for row in rows:
        by_participant[row["participant_id"]].append(row)
    regularity = []
    completeness = []
    for participant in sorted(by_participant):
        participant_rows = sorted(by_participant[participant], key=lambda row: row["cutoff"])
        regularity.append({"participant_id": participant, **_participant_regularity(participant_rows)})
        completeness.append({"participant_id": participant, **_participant_completeness(participant_rows)})
    return {
        "role": "descriptive_observed_data",
        "note": "관측 기록의 기술통계입니다. 모델 정확도·임상 지표·추천 효과가 아닙니다.",
        "assumptions": ASSUMPTIONS,
        "regularity": regularity,
        "completeness": completeness,
    }
