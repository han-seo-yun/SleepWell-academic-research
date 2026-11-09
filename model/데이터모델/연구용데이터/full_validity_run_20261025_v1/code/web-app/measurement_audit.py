"""Aggregate measurement-process audit for SleepWell phone records.

The current Android record contract contains sleep timestamps, a declared duration,
source, and modification time.  It does not contain wear time, device/OS metadata,
PSG scoring, or a shift-work measure.  This module keeps those dimensions explicitly
unavailable rather than inferring them from missing records or clock patterns.

All outputs are aggregate.  Record, participant, session, and device identifiers are
used only for deterministic de-duplication and are never returned.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import json
import math
from statistics import fmean, median
from zoneinfo import ZoneInfo


ZONE = ZoneInfo("Asia/Seoul")
DIARY_SOURCES = frozenset({"manual", "diary", "user_entry"})
WEARABLE_SOURCES = frozenset({"samsung_health", "health_connect", "healthkit"})
PSG_SOURCES = frozenset({"psg"})
KNOWN_SOURCES = DIARY_SOURCES | WEARABLE_SOURCES | PSG_SOURCES
REQUIRED_RECORD_FIELDS = frozenset(
    {"id", "day", "bedtime", "wake_time", "duration_minutes", "source", "modified_at"}
)
MIN_PERIOD_RECORDS = 3
MAX_RECORDS = 10_000
MAX_SNAPSHOTS = 2_000


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field}_not_string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError(f"{field}_timezone_naive")
    return parsed.astimezone(ZONE)


def _as_of(value: datetime | str) -> datetime:
    if isinstance(value, str):
        return _aware(value, "as_of")
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("as_of_must_be_timezone_aware")
    return value.astimezone(ZONE)


def _source_role(source: str) -> str:
    if source in DIARY_SOURCES:
        return "diary_primary"
    if source in WEARABLE_SOURCES:
        return "wearable_auxiliary"
    if source in PSG_SOURCES:
        return "psg_timestamp_only"
    return "unknown"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                      separators=(",", ":"), allow_nan=False)


def _clock_min(value: datetime) -> float:
    return value.hour * 60 + value.minute + value.second / 60


def _clock_center(values: list[float]) -> float | None:
    if not values:
        return None
    angles = [2 * math.pi * value / 1440 for value in values]
    x = fmean(math.cos(value) for value in angles)
    y = fmean(math.sin(value) for value in angles)
    if math.hypot(x, y) < 1e-12:
        return None
    return round((math.atan2(y, x) * 1440 / (2 * math.pi)) % 1440, 3)


def _clock_delta(after: float | None, before: float | None) -> float | None:
    if after is None or before is None:
        return None
    return round((after - before + 720) % 1440 - 720, 3)


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _validate_records(records: list[dict], now: datetime) -> tuple[list[dict], dict]:
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ValueError("records_must_be_bounded_list")
    counts = Counter()
    valid = []
    for raw in records:
        if not isinstance(raw, dict) or not REQUIRED_RECORD_FIELDS.issubset(raw):
            counts["missing_or_invalid_record"] += 1
            continue
        try:
            identity = raw["id"]
            source = str(raw["source"]).strip().lower()
            if not isinstance(identity, str) or not identity or len(identity) > 256:
                raise ValueError("identity")
            if not isinstance(raw["day"], str):
                raise ValueError("day")
            date.fromisoformat(raw["day"])
            bed = _aware(raw["bedtime"], "bedtime")
            wake = _aware(raw["wake_time"], "wake_time")
            modified = _aware(raw["modified_at"], "modified_at")
            declared = raw["duration_minutes"]
            if (isinstance(declared, bool) or not isinstance(declared, int)
                    or not 0 <= declared <= 1440):
                raise ValueError("duration")
        except (ValueError, TypeError, OverflowError):
            counts["invalid_record"] += 1
            continue
        if wake <= bed:
            counts["nonpositive_interval"] += 1
            continue
        if wake > now or modified > now:
            counts["not_yet_available"] += 1
            continue
        interval = (wake - bed).total_seconds() / 60
        if interval > 1440:
            counts["interval_over_24h"] += 1
            continue
        if wake.date().isoformat() != raw["day"]:
            counts["declared_day_differs_from_kst_wake_date"] += 1
        role = _source_role(source)
        if role == "unknown":
            counts["unknown_source"] += 1
            continue
        # Extra fields are retained nowhere.  Current Android serialization has no
        # registered device, OS, wear-time, or PSG-scoring metadata contract.
        if set(raw) - REQUIRED_RECORD_FIELDS - {"rested", "fatigue"}:
            counts["records_with_ignored_unregistered_fields"] += 1
        valid.append({
            "identity": identity, "source": source, "role": role,
            "day": wake.date(), "bed": bed, "wake": wake, "modified": modified,
            "declared": float(declared), "interval": interval,
            "canonical": _canonical(raw),
        })
    counts["input"] = len(records)
    counts["valid_known_source"] = len(valid)
    counts["valid_diary"] = sum(item["role"] == "diary_primary" for item in valid)
    counts["valid_wearable"] = sum(item["role"] == "wearable_auxiliary" for item in valid)
    counts["valid_psg_timestamp_only"] = sum(item["role"] == "psg_timestamp_only" for item in valid)
    return valid, dict(sorted(counts.items()))


def _latest_record_versions(records: list[dict]) -> tuple[list[dict], int]:
    latest = {}
    for item in records:
        key = (item["source"], item["identity"])
        candidate = (item["modified"], item["canonical"], item)
        if key not in latest or candidate[:2] > latest[key][:2]:
            latest[key] = candidate
    return [row[2] for row in latest.values()], len(records) - len(latest)


def _enrollment_coverage(records: list[dict], profile: dict | None, now: datetime) -> dict:
    value = profile.get("created_at") if isinstance(profile, dict) else None
    try:
        enrolled = _aware(value, "profile.created_at")
    except (ValueError, TypeError, OverflowError):
        return {"available": False, "reason": "valid_timezone_aware_profile_created_at_missing",
                "interpretation": "Enrollment-denominator coverage was not estimated."}
    if enrolled > now:
        return {"available": False, "reason": "enrollment_after_as_of",
                "interpretation": "Enrollment-denominator coverage was not estimated."}
    denominator = (now.date() - enrolled.date()).days + 1
    eligible = [item for item in records if item["wake"] >= enrolled]
    by_role, by_source = {}, {}
    for label, subset in (
        *((role, [x for x in eligible if x["role"] == role])
          for role in ("diary_primary", "wearable_auxiliary", "psg_timestamp_only")),
        *((source, [x for x in eligible if x["source"] == source])
          for source in sorted(KNOWN_SOURCES)),
    ):
        days = len({item["day"] for item in subset})
        target = by_role if label in {"diary_primary", "wearable_auxiliary", "psg_timestamp_only"} else by_source
        target[label] = {"recorded_wake_days": days, "enrollment_days": denominator,
                         "recorded_day_fraction": round(days / denominator, 3)}
    return {
        "available": True, "enrollment_date": enrolled.date().isoformat(),
        "as_of_date": now.date().isoformat(), "denominator_days": denominator,
        "records_before_enrollment_excluded": len(records) - len(eligible),
        "by_role": by_role, "by_source": by_source,
        "interpretation": (
            "Fraction of enrollment calendar days with a known-source wake record; "
            "not sensor wear adherence, non-wear, or proof that sleep did not occur."
        ),
    }


def _duration_semantics(records: list[dict]) -> dict:
    differences = [item["declared"] - item["interval"] for item in records]
    absolute = [abs(value) for value in differences]
    return {
        "n_records": len(records),
        "median_declared_minus_timestamp_interval_min": round(median(differences), 3) if differences else None,
        "median_absolute_difference_min": round(median(absolute), 3) if absolute else None,
        "over_5_min_difference_count": sum(value > 5 for value in absolute),
        "interpretation": (
            "Declared duration and bedtime-to-wake interval can represent different constructs. "
            "Differences are descriptive and are not labeled sleep efficiency, error, or low quality."
        ),
    }


def _overlap(left: dict, right: dict) -> bool:
    return max(left["bed"], right["bed"]) < min(left["wake"], right["wake"])


def _episode_selection(records: list[dict]) -> dict:
    by_day = defaultdict(list)
    for item in records:
        by_day[item["day"]].append(item)
    multi = overlap_days = 0
    longest_latest = longest_diary = latest_diary = 0
    for episodes in by_day.values():
        if len(episodes) < 2:
            continue
        multi += 1
        if any(_overlap(left, right) for index, left in enumerate(episodes)
               for right in episodes[index + 1:]):
            overlap_days += 1
        latest = max(episodes, key=lambda x: (x["modified"], x["canonical"]))
        longest = max(episodes, key=lambda x: (x["interval"], x["modified"], x["canonical"]))
        diaries = [item for item in episodes if item["role"] == "diary_primary"]
        diary_priority = max(diaries or episodes,
                             key=lambda x: (x["modified"], x["interval"], x["canonical"]))
        longest_latest += longest["identity"] != latest["identity"] or longest["source"] != latest["source"]
        longest_diary += longest["identity"] != diary_priority["identity"] or longest["source"] != diary_priority["source"]
        latest_diary += latest["identity"] != diary_priority["identity"] or latest["source"] != diary_priority["source"]
    return {
        "wake_dates": len(by_day), "multiple_episode_wake_dates": multi,
        "overlapping_episode_wake_dates": overlap_days,
        "policy_disagreement_wake_dates": {
            "longest_vs_latest_modified": longest_latest,
            "longest_vs_diary_priority_latest": longest_diary,
            "latest_modified_vs_diary_priority_latest": latest_diary,
        },
        "policies": {
            "longest": "largest timestamp interval; ties use later modification and canonical ordering",
            "latest_modified": "latest modification; ties use canonical ordering",
            "diary_priority_latest": "latest diary when present, otherwise latest known-source episode",
        },
        "interpretation": (
            "Sensitivity flags only. Multiple episodes are not automatically naps, fragmentation, "
            "or errors, and no policy is used to overwrite the original record."
        ),
    }


def _period(records: list[dict], start: date, end: date) -> dict:
    # At most one latest record per source and wake date for a bounded 14-day count.
    latest = {}
    for item in records:
        if not start <= item["day"] <= end:
            continue
        key = item["day"]
        candidate = (item["modified"], item["canonical"], item)
        if key not in latest or candidate[:2] > latest[key][:2]:
            latest[key] = candidate
    selected = [value[2] for value in latest.values()]
    supported = len(selected) >= MIN_PERIOD_RECORDS
    return {
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "expected_days": 14, "recorded_days": len(selected),
        "minimum_records_for_descriptive_center": MIN_PERIOD_RECORDS,
        "descriptive_center_available": supported,
        "bedtime_circular_center_min": _clock_center([_clock_min(x["bed"]) for x in selected]) if supported else None,
        "wake_circular_center_min": _clock_center([_clock_min(x["wake"]) for x in selected]) if supported else None,
        "declared_duration_mean_min": round(fmean(x["declared"] for x in selected), 3) if supported else None,
    }


def _fixed_periods(records: list[dict], profile: dict | None, now: datetime) -> dict:
    try:
        enrolled = _aware(profile.get("created_at"), "profile.created_at") if isinstance(profile, dict) else None
    except (ValueError, TypeError, OverflowError):
        enrolled = None
    if enrolled is None or enrolled > now:
        return {"available": False, "reason": "valid_enrollment_required"}
    first_start, first_end = enrolled.date(), enrolled.date() + timedelta(days=13)
    recent_end, recent_start = now.date(), now.date() - timedelta(days=13)
    nonoverlap = recent_start > first_end
    output = {}
    for source in sorted(KNOWN_SOURCES):
        source_records = [item for item in records if item["source"] == source]
        first = _period(source_records, first_start, first_end)
        recent = _period(source_records, recent_start, recent_end)
        comparable = (nonoverlap and first["descriptive_center_available"]
                      and recent["descriptive_center_available"])
        output[source] = {
            "first_14_days": first, "recent_14_days": recent,
            "periods_nonoverlapping": nonoverlap,
            "descriptive_difference_available": comparable,
            "recent_minus_first": ({
                "bedtime_center_min": _clock_delta(recent["bedtime_circular_center_min"], first["bedtime_circular_center_min"]),
                "wake_center_min": _clock_delta(recent["wake_circular_center_min"], first["wake_circular_center_min"]),
                "declared_duration_mean_min": round(recent["declared_duration_mean_min"] - first["declared_duration_mean_min"], 3),
            } if comparable else None),
        }
    return {
        "available": True, "window_definition": "fixed first 14 enrollment days versus as-of ending 14 days",
        "by_source": output,
        "interpretation": (
            "Descriptive source-specific period comparison. Differences can reflect sample composition, "
            "capture, device processing, adherence, or behavior and do not establish causal drift."
        ),
    }


def _snapshot_lag(snapshots: list[dict] | None, now: datetime) -> dict:
    if snapshots is None:
        return {"available": False, "reason": "append_only_snapshot_history_not_supplied",
                "modified_at_used": False}
    if not isinstance(snapshots, list) or len(snapshots) > MAX_SNAPSHOTS:
        raise ValueError("snapshots_must_be_bounded_list")
    accepted, invalid, future, conflicts = {}, 0, 0, 0
    keys = {}
    participants = set()
    for raw in snapshots:
        try:
            if not isinstance(raw, dict) or type(raw.get("revision")) is not int or raw["revision"] < 1:
                raise ValueError("snapshot")
            participant = raw["participant_key"]
            if not isinstance(participant, str) or not participant:
                raise ValueError("participant")
            day = date.fromisoformat(raw["day"])
            generated = _aware(raw["generated_at"], "generated_at")
            received = _aware(raw["received_at"], "received_at") if raw.get("received_at") else None
            data = raw["data"]
            if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                raise ValueError("data")
        except (KeyError, ValueError, TypeError, OverflowError):
            invalid += 1
            continue
        if generated > now or (received is not None and received > now):
            future += 1
            continue
        key = (participant, day, raw["revision"])
        # A revision is immutable.  ``received_at`` is transport metadata and may
        # differ for an idempotent retry, but generation time and payload may not.
        canonical = _canonical({"generated_at": generated.isoformat(), "data": data})
        if key in keys and keys[key] is not None and keys[key] != canonical:
            conflicts += 1
            keys[key] = None
            accepted.pop(key, None)
            continue
        if key in keys and keys[key] is None:
            continue
        if key in keys:
            old = accepted[key]
            old_received = old[1]
            if received is not None and (old_received is None or received < old_received):
                accepted[key] = (generated, received, data["records"])
            continue
        keys[key] = canonical
        participants.add(participant)
        accepted[key] = (generated, received, data["records"])
    if len(participants) > 1:
        return {"available": False, "reason": "multiple_participants_require_separate_audits",
                "accepted_snapshot_versions": len(accepted), "invalid_snapshot_versions": invalid,
                "future_snapshot_versions_excluded": future, "conflicting_revision_keys": conflicts,
                "modified_at_used": False}
    appearances = {}
    for generated, received, records in accepted.values():
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                continue
            try:
                wake = _aware(record["wake_time"], "wake_time")
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
            if wake > generated:
                continue
            identity = record["id"]
            candidate = (generated, received, wake)
            if identity not in appearances or generated < appearances[identity][0]:
                appearances[identity] = candidate
    generation_lags = [(generated - wake).total_seconds() / 60
                       for generated, _, wake in appearances.values()]
    receipt_lags = [(received - wake).total_seconds() / 60
                    for generated, received, wake in appearances.values() if received is not None and received >= generated]
    return {
        "available": bool(appearances),
        "reason": None if appearances else "no_temporally_valid_first_appearance_events",
        "accepted_snapshot_versions": len(accepted),
        "invalid_snapshot_versions": invalid,
        "future_snapshot_versions_excluded": future,
        "conflicting_revision_keys": conflicts,
        "records_with_first_appearance": len(appearances),
        "first_snapshot_generation_lag_median_min": round(median(generation_lags), 3) if generation_lags else None,
        "first_pc_receipt_lag_median_min": round(median(receipt_lags), 3) if receipt_lags else None,
        "modified_at_used": False,
        "interpretation": (
            "Wake-to-first appearance in the supplied append-only backup history. Generation lag is "
            "not guaranteed to equal first user entry; PC receipt lag also includes delivery delay."
        ),
    }


def _direct_context(profile: dict | None, context: dict | None, now: datetime) -> dict:
    contexts = []
    candidates = []
    if isinstance(profile, dict) and isinstance(profile.get("daily_context"), dict):
        candidates.append(profile["daily_context"])
    if isinstance(context, dict):
        if isinstance(context.get("daily_contexts"), list):
            candidates.extend(context["daily_contexts"])
        elif "night_id" in context:
            candidates.append(context)
    invalid = 0
    nap_count = nap_minutes = 0.0
    for value in candidates:
        try:
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValueError("schema")
            observed = _aware(value["observed_at"], "observed_at")
            if observed > now or not isinstance(value.get("naps", []), list):
                raise ValueError("availability")
            valid_naps = []
            for nap in value.get("naps", []):
                start = _aware(nap["start"], "nap.start")
                end = _aware(nap["end"], "nap.end")
                duration = (end - start).total_seconds() / 60
                if not 0 < duration <= 240 or end > observed:
                    raise ValueError("nap")
                valid_naps.append(duration)
        except (KeyError, ValueError, TypeError, OverflowError):
            invalid += 1
            continue
        contexts.append(value)
        nap_count += len(valid_naps)
        nap_minutes += sum(valid_naps)
    sync = context.get("mobile_sync_summary") if isinstance(context, dict) else None
    safe_sync = None
    if isinstance(sync, dict):
        fields = ("sessions_total", "sessions_eligible", "records_held", "conflicts_pending")
        reasons = sync.get("held_reasons")
        if (all(type(sync.get(field)) is int and sync[field] >= 0 for field in fields)
                and isinstance(reasons, dict)
                and all(isinstance(key, str) and type(value) is int and value >= 0
                        for key, value in reasons.items())):
            allowed = {key: reasons.get(key, 0) for key in
                       ("stage_gap", "unknown_stage", "short_sleep", "no_asleep_stage")}
            safe_sync = {field: sync[field] for field in fields}
            safe_sync["held_reasons"] = allowed
            safe_sync["interpretation"] = (
                "Importer eligibility and held-session counts; not wear time, sensor accuracy, or sleep absence."
            )
    return {
        "valid_daily_context_snapshots": len(contexts),
        "invalid_or_future_daily_context_snapshots": invalid,
        "reported_completed_naps": int(nap_count),
        "reported_completed_nap_minutes": round(nap_minutes, 3),
        "nap_interpretation": "Explicit validated self-reported naps only; extra sleep episodes are not relabeled as naps.",
        "mobile_sync_ingestion": safe_sync or {"available": False, "reason": "validated_mobile_sync_summary_not_supplied"},
    }


def audit_measurements(records: list[dict], as_of: datetime | str, profile: dict | None = None,
                       snapshots: list[dict] | None = None, context: dict | None = None) -> dict:
    """Return a bounded, identifier-free audit of the measurement process."""
    now = _as_of(as_of)
    valid, validation = _validate_records(records, now)
    latest, duplicate_versions = _latest_record_versions(valid)
    return {
        "schema_version": 1,
        "artifact_type": "sleepwell_measurement_audit",
        "generated_as_of": now.isoformat(),
        "analysis_scope": "aggregate_measurement_process_audit_no_relabeling",
        "record_validation": {**validation, "superseded_versions": duplicate_versions,
                              "latest_known_episode_versions": len(latest)},
        "enrollment_coverage": _enrollment_coverage(latest, profile, now),
        "duration_semantics": _duration_semantics(latest),
        "episode_selection_sensitivity": _episode_selection(latest),
        "fixed_14_day_periods": _fixed_periods(latest, profile, now),
        "first_snapshot_appearance_lag": _snapshot_lag(snapshots, now),
        "direct_context": _direct_context(profile, context, now),
        "quality_subset_comparison": {
            "available": False,
            "reason": "current_sleep_record_contract_has_no_registered_per-record_wear_or_quality_flag",
            "all_records_n": len(latest),
            "quality_filtered_n": None,
        },
        "unavailable_dimensions": {
            "wear_time": "not_in_current_android_record_contract",
            "device_model": "not_in_current_android_record_contract",
            "os_version": "not_in_current_android_record_contract",
            "sensor_nonwear": "cannot_be_inferred_from_missing_sleep_records",
            "psg_stage_or_scoring_gold_standard": (
                "unavailable" if not any(x["role"] == "psg_timestamp_only" for x in latest)
                else "timestamp_records_present_but_scoring_and_alignment_contract_unavailable"
            ),
            "shift_work": "not_measured_and_not_inferred_from_occupation_or_clock_timing",
        },
        "limitations": [
            "Source and period differences do not identify which source is correct or prove device drift.",
            "Episode-selection sensitivity does not classify naps, main sleep, fragmentation, or errors.",
            "No record is removed, overwritten, relabeled, or promoted into model training by this audit.",
            "Performance on all versus quality-filtered nights requires a prospectively registered quality contract.",
        ],
    }
