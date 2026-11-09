"""Aggregate target-validity diagnostics for phone sleep records.

This module audits the observations used around the prediction pipeline.  It never
changes, filters, or re-labels records used by a model, and it returns no row-level
records or identifiers.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import math
from statistics import fmean, median
from zoneinfo import ZoneInfo


ZONE = ZoneInfo("Asia/Seoul")
DIARY_SOURCES = frozenset({"manual", "diary", "user_entry"})
WEARABLE_SOURCES = frozenset({"health_connect", "healthkit", "samsung_health"})
KNOWN_SOURCES = DIARY_SOURCES | WEARABLE_SOURCES
REQUIRED_FIELDS = frozenset(
    {"id", "day", "bedtime", "wake_time", "duration_minutes", "source", "modified_at"}
)
DESCRIPTIVE_QUANTILE_MIN_PAIRS = 20


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp_not_string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise TypeError("timestamp_timezone_naive")
    return parsed.astimezone(ZONE)


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _round(value: float) -> float:
    return round(float(value), 3)


def _series_summary(signed_values: list[float]) -> dict:
    if not signed_values:
        return {
            "median_absolute_difference_min": None,
            "mean_signed_difference_min": None,
            "proportion_absolute_over_30_min": None,
            "proportion_absolute_over_60_min": None,
            "signed_difference_quantiles_2_5_97_5_min": None,
        }
    absolute = [abs(value) for value in signed_values]
    quantiles = None
    if len(signed_values) >= DESCRIPTIVE_QUANTILE_MIN_PAIRS:
        quantiles = [
            _round(_quantile(signed_values, 0.025)),
            _round(_quantile(signed_values, 0.975)),
        ]
    return {
        "median_absolute_difference_min": _round(median(absolute)),
        "mean_signed_difference_min": _round(fmean(signed_values)),
        "proportion_absolute_over_30_min": _round(
            sum(value > 30 for value in absolute) / len(absolute)
        ),
        "proportion_absolute_over_60_min": _round(
            sum(value > 60 for value in absolute) / len(absolute)
        ),
        "signed_difference_quantiles_2_5_97_5_min": quantiles,
    }


def _agreement(pairs: list[tuple[dict, dict]]) -> dict:
    onset = [(device["bed"] - diary["bed"]).total_seconds() / 60 for diary, device in pairs]
    wake = [(device["wake"] - diary["wake"]).total_seconds() / 60 for diary, device in pairs]
    return {
        "paired_nights": len(pairs),
        "difference_direction": "wearable_minus_diary",
        "onset": _series_summary(onset),
        "wake": _series_summary(wake),
        "quantile_policy": {
            "type": "empirical_linear_descriptive",
            "minimum_pairs": DESCRIPTIVE_QUANTILE_MIN_PAIRS,
            "available": len(pairs) >= DESCRIPTIVE_QUANTILE_MIN_PAIRS,
        },
    }


def summarize_target_validity(records: list[dict], as_of: datetime | str) -> dict:
    """Return aggregate observation-quality and diary/wearable agreement diagnostics.

    Coverage uses the inclusive KST wake-date span of all valid known-source records.
    It is an observed-data density measure, not enrollment adherence.  Agreement is
    calculated only for unambiguous, overlapping intervals on the same KST wake date.
    """

    as_of_value = _timestamp(as_of) if isinstance(as_of, str) else as_of
    if not isinstance(as_of_value, datetime) or as_of_value.utcoffset() is None:
        raise ValueError("as_of_must_be_timezone_aware")
    as_of_value = as_of_value.astimezone(ZONE)

    exclusions = Counter()
    valid: list[dict] = []
    source_input_counts = Counter()
    update_lags: list[float] = []

    for record in records:
        if not isinstance(record, dict) or not REQUIRED_FIELDS.issubset(record):
            exclusions["invalid_record_count"] += 1
            exclusions["missing_required_field_count"] += 1
            continue
        source = str(record["source"]).strip().lower()
        source_input_counts[source] += 1
        if source not in KNOWN_SOURCES:
            exclusions["unknown_source_count"] += 1
            continue
        try:
            bed = _timestamp(record["bedtime"])
            wake = _timestamp(record["wake_time"])
            modified = _timestamp(record["modified_at"])
        except TypeError:
            exclusions["invalid_record_count"] += 1
            exclusions["timezone_naive_count"] += 1
            continue
        except (ValueError, OverflowError):
            exclusions["invalid_record_count"] += 1
            exclusions["timestamp_parse_error_count"] += 1
            continue
        if wake <= bed:
            exclusions["invalid_record_count"] += 1
            exclusions["nonpositive_interval_count"] += 1
            continue
        if wake > as_of_value or modified > as_of_value:
            exclusions["future_record_count"] += 1
            continue
        if modified < wake:
            exclusions["invalid_record_count"] += 1
            exclusions["modified_before_wake_count"] += 1
            continue
        declared_duration = record["duration_minutes"]
        if (isinstance(declared_duration, bool)
                or not isinstance(declared_duration, (int, float))
                or not math.isfinite(float(declared_duration))
                or not 0 <= float(declared_duration) <= 1440):
            exclusions["invalid_record_count"] += 1
            exclusions["invalid_declared_duration_count"] += 1
            continue
        wake_day = wake.date().isoformat()
        update_lags.append((modified - wake).total_seconds() / 60)
        valid.append({"source": source, "bed": bed, "wake": wake, "wake_day": wake_day})

    valid_dates = [item["wake"].date() for item in valid]
    span_days = ((max(valid_dates) - min(valid_dates)).days + 1) if valid_dates else 0
    source_days: dict[str, set[str]] = defaultdict(set)
    role_days = {"diary_primary": set(), "wearable_auxiliary": set()}
    for item in valid:
        source_days[item["source"]].add(item["wake_day"])
        role = "diary_primary" if item["source"] in DIARY_SOURCES else "wearable_auxiliary"
        role_days[role].add(item["wake_day"])

    def coverage(days: set[str]) -> dict:
        return {
            "observed_wake_days": len(days),
            "observed_span_days": span_days,
            "observed_day_coverage": _round(len(days) / span_days) if span_days else None,
        }

    diary_by_day: dict[str, list[dict]] = defaultdict(list)
    wearable_by_source_day: dict[str, dict[str, list[dict]]] = {
        source: defaultdict(list) for source in sorted(WEARABLE_SOURCES)
    }
    for item in valid:
        if item["source"] in DIARY_SOURCES:
            diary_by_day[item["wake_day"]].append(item)
        else:
            wearable_by_source_day[item["source"]][item["wake_day"]].append(item)

    agreements = {}
    ambiguous_days: set[str] = set()
    nonoverlap_days: set[str] = set()
    for source in sorted(WEARABLE_SOURCES):
        pairs: list[tuple[dict, dict]] = []
        source_ambiguous: set[str] = set()
        source_nonoverlap: set[str] = set()
        candidate_days = set(diary_by_day) & set(wearable_by_source_day[source])
        for wake_day in sorted(candidate_days):
            diaries = diary_by_day[wake_day]
            wearables = wearable_by_source_day[source][wake_day]
            if len(diaries) != 1 or len(wearables) != 1:
                source_ambiguous.add(wake_day)
                ambiguous_days.add(wake_day)
                continue
            diary, device = diaries[0], wearables[0]
            if max(diary["bed"], device["bed"]) >= min(diary["wake"], device["wake"]):
                source_nonoverlap.add(wake_day)
                nonoverlap_days.add(wake_day)
                continue
            pairs.append((diary, device))
        agreements[source] = {
            **_agreement(pairs),
            "candidate_same_wake_date_nights": len(candidate_days),
            "ambiguous_duplicate_nights_excluded": len(source_ambiguous),
            "nonoverlapping_interval_nights_excluded": len(source_nonoverlap),
        }

    update_lag_summary = {
        "interpretation": (
            "modified_at minus wake_time; this is update lag and may include later edits, "
            "not the first reporting delay"
        ),
        "n_records": len(update_lags),
        "median_min": _round(median(update_lags)) if update_lags else None,
        "maximum_min": _round(max(update_lags)) if update_lags else None,
    }
    valid_source_counts = Counter(item["source"] for item in valid)
    wearable_valid_count = sum(valid_source_counts[source] for source in WEARABLE_SOURCES)
    return {
        "schema_version": 1,
        "artifact_type": "sleepwell_target_validity_summary",
        "generated_as_of": as_of_value.isoformat(),
        "analysis_scope": "aggregate_observation_audit_only",
        "record_counts": {
            "input": len(records),
            "valid_known_source": len(valid),
            "valid_by_source": dict(sorted(valid_source_counts.items())),
            "input_by_source": dict(sorted(source_input_counts.items())),
        },
        "exclusions": {
            key: exclusions.get(key, 0)
            for key in (
                "invalid_record_count", "missing_required_field_count", "timezone_naive_count",
                "timestamp_parse_error_count", "nonpositive_interval_count", "future_record_count",
                "modified_before_wake_count", "invalid_declared_duration_count", "unknown_source_count",
            )
        },
        "coverage": {
            "denominator": "inclusive_span_between_first_and_last_valid_known_source_KST_wake_dates",
            "interpretation": "observed-data density; not enrollment adherence",
            "span_start_wake_date": min(valid_dates).isoformat() if valid_dates else None,
            "span_end_wake_date": max(valid_dates).isoformat() if valid_dates else None,
            "by_source": {source: coverage(source_days[source]) for source in sorted(KNOWN_SOURCES)},
            "by_role": {role: coverage(days) for role, days in role_days.items()},
        },
        "agreement": {
            "pairing_rule": (
                "exactly one diary and one record from the named wearable source on the same KST "
                "wake date, with overlapping sleep intervals"
            ),
            "by_wearable_source": agreements,
            "ambiguous_wake_dates_excluded": len(ambiguous_days),
            "nonoverlapping_wake_dates_excluded": len(nonoverlap_days),
            "interpretation": "descriptive source agreement; not accuracy against a gold standard",
        },
        "update_lag": update_lag_summary,
        "wear_quality": {
            "status": "unavailable",
            "valid_wearable_records_without_wear_quality": wearable_valid_count,
            "interpretation": "wear time and device quality flags are not present in this record contract",
        },
        "evidence_status": {
            "psg_gold_standard": "unavailable",
            "actual_sleep_quality_measured": False,
            "sleep_pressure_measured": False,
            "prediction_accuracy_measured_here": False,
            "recommendation_effect_measured_here": False,
        },
        "limitations": [
            "No PSG gold standard is present, so source differences do not identify which source is correct.",
            "modified_at can reflect an edit; update lag is not first-report latency.",
            "Coverage is bounded by the observed valid-record span and is not enrollment adherence.",
            "This audit does not remove records from model training, prediction, or outcome evaluation.",
        ],
    }
