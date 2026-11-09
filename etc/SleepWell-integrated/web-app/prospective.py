"""Prospective next-night forecasting and evaluation helpers.

Forecasts are immutable, timestamped snapshots made from information available at
the local 20:00 decision.  This module has no persistence dependency: the server
stores forecast and outcome versions and decides which first outcome is primary.
The policy is an unvalidated behavioral forecast, not a treatment effect model.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from statistics import mean, median

import engine
from night_core import CONTRACT as FUSION_FEATURE_CONTRACT
from night_core import cutoff_for_onset, local_time, normalize_history, constrain


MODEL_VERSION = "prospective-behavior-v4-locked-monitoring"
FEATURE_CONTRACT = "sleepwell_prospective_pre20_v4_locked_monitoring"
V2_MODEL_VERSION = "prospective-behavior-v2"
V2_FEATURE_CONTRACT = "sleepwell_prospective_pre20_v2"
LEGACY_MODEL_VERSION = "prospective-behavior-v1"
LEGACY_FEATURE_CONTRACT = "sleepwell_prospective_pre20_v1"
SELECTED_MODEL = "recent_adaptive"
BASE_ARMS = frozenset(("profile_baseline", "recent_adaptive", "context_candidate"))
FUSION_ARMS = frozenset((
    "physical_cmi", "data_cmi", "hybrid_cmi",
    "physical_mesa", "data_mesa", "hybrid_mesa",
))
# Kept as a compatibility export for callers that only know the original trial.
(EXPECTED_ARMS, V3_MODEL_VERSION, V3_FEATURE_CONTRACT,
 MONITORING_PROTOCOL_ID, MONITORING_SCHEMA_VERSION,
 INTERVAL_CALIBRATION_SCHEMA_VERSION, INTERVAL_METHOD,
 MIN_CALIBRATION_N, NOMINAL_JOINT_COVERAGE, CALIBRATION_DAYS,
 HELDOUT_DAYS, OUTCOME_SUPPORT, ERROR_METRICS) = (
    BASE_ARMS,
    "prospective-behavior-v3-daily-personalization",
    "sleepwell_prospective_pre20_v3_daily_personalization",
    "sleepwell-prospective-interval-monitoring-v1",
    1,
    1,
    "paired_max_absolute_residual_finite_sample_q90_v1",
    20,
    0.90,
    30,
    30,
    {
        "onset_offset_min": {"lower": 0.0, "upper": 960.0},
        "wake_offset_min": {"lower": 30.0, "upper": 1440.0},
        "interval_min": {"lower": 30.0, "upper": 960.0},
    },
    ("onset_min", "wake_min", "interval_min", "joint_min"),
)


def _snapshot(value):
    """JSON round-trip makes callers unable to mutate a stored input snapshot."""
    _finite(value)
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default))


def _json_default(value):
    if isinstance(value, datetime):
        return local_time(value).isoformat()
    raise TypeError(f"Unsupported prospective value: {type(value).__name__}")


def _finite(value, path="input"):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} contains a non-finite number")
    if isinstance(value, dict):
        for key, item in value.items():
            _finite(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _finite(item, f"{path}[{index}]")


def _clock(value):
    if isinstance(value, (int, float)):
        minute = float(value)
    else:
        hour, minute_part = map(int, str(value).split(":"))
        minute = hour * 60 + minute_part
    if not math.isfinite(minute) or not 0 <= minute < 1440:
        raise ValueError("Invalid clock time")
    return minute


def _offset(clock_minute):
    return (float(clock_minute) - 1200) % 1440


def _iso_prediction(cutoff, bed, wake):
    bed, wake = constrain(bed, wake)
    return {
        "bed": (cutoff + timedelta(minutes=bed)).isoformat(),
        "wake": (cutoff + timedelta(minutes=wake)).isoformat(),
        "onset_offset_min": bed,
        "wake_offset_min": wake,
        "interval_min": wake - bed,
    }


def _canonical_hash(value):
    _finite(value)
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_sha256(value, label):
    if (not isinstance(value, str) or len(value) != 64
            or value != value.lower()
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError(f"{label} must be a canonical lowercase SHA-256")
    return value


def _explicit_latency(record):
    raw = record.get("latency_min")
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ValueError("latency_min is invalid")
    value = float(raw)
    if not math.isfinite(value) or not 0 <= value <= 180:
        raise ValueError("latency_min is invalid")
    return value


def _recorded_onset(record):
    """Return actual/derived onset without treating missing latency as zero."""
    latency = _explicit_latency(record)
    raw_onset = record.get("sleep_start")
    if raw_onset is not None and raw_onset != "":
        return local_time(raw_onset), "explicit_sleep_start", latency
    if latency is None:
        return None, "unavailable_missing_sleep_start_and_latency", None
    return (local_time(record["bed"]) + timedelta(minutes=latency),
            "derived_from_bed_plus_explicit_latency", latency)


def known_onset_records(record_versions):
    """Keep raw records with a valid explicit or finite-latency onset."""
    known = []
    for record in record_versions or []:
        if not isinstance(record, dict):
            continue
        try:
            onset, _provenance, _latency = _recorded_onset(record)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if onset is not None:
            known.append(record)
    return known


def _causal_diary_records(records, cutoff, enrolled_at):
    """Preserve raw measurements while enforcing strict pre-cutoff availability."""
    enrollment = local_time(enrolled_at)
    causal = []
    for record in records or []:
        if not isinstance(record, dict) or record.get("source") != "diary":
            continue
        try:
            wake = local_time(record["wake"])
            observed = local_time(record["observed_at"])
            _explicit_latency(record)
            if record.get("sleep_start") not in (None, ""):
                local_time(record["sleep_start"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if enrollment <= wake < cutoff and observed < cutoff:
            causal.append(_snapshot(record))
    return causal


def _next_local_20_strictly_after(value):
    registered = local_time(value)
    candidate = registered.replace(hour=20, minute=0, second=0, microsecond=0)
    return candidate if candidate > registered else candidate + timedelta(days=1)


def create_monitoring_lock(registered_at, enrollment_id, artifact_identity_sha256):
    """Create the immutable preset for future calibration and held-out monitoring."""
    registered = local_time(registered_at)
    enrollment = str(enrollment_id).strip()
    if not enrollment:
        raise ValueError("enrollment_id is required")
    artifact_identity = _canonical_sha256(
        artifact_identity_sha256, "artifact_identity_sha256")
    future_start = _next_local_20_strictly_after(registered)
    calibration_end = future_start + timedelta(days=CALIBRATION_DAYS)
    heldout_end = calibration_end + timedelta(days=HELDOUT_DAYS)
    lock = {
        "schema_version": MONITORING_SCHEMA_VERSION,
        "protocol_id": MONITORING_PROTOCOL_ID,
        "registered_at": registered.isoformat(),
        "enrollment_id": enrollment,
        "selected_model": SELECTED_MODEL,
        "primary_arm": SELECTED_MODEL,
        "model_version": MODEL_VERSION,
        "feature_contract": FEATURE_CONTRACT,
        "engine_parameter_version": engine.PARAMETER_VERSION,
        "artifact_identity_sha256": artifact_identity,
        "future_period_start": future_start.isoformat(),
        "calibration_end": calibration_end.isoformat(),
        "heldout_end": heldout_end.isoformat(),
        "min_calibration_n": MIN_CALIBRATION_N,
        "nominal_joint_coverage": NOMINAL_JOINT_COVERAGE,
        "interval_method": INTERVAL_METHOD,
        "outcome_support": _snapshot(OUTCOME_SUPPORT),
        "randomization": None,
        "purpose": "forecast_monitoring_not_human_randomization",
    }
    lock["monitoring_lock_sha256"] = _canonical_hash(lock)
    return lock


def _validated_monitoring_lock(value):
    if not isinstance(value, dict):
        raise ValueError("monitoring_lock must be an object")
    lock = _snapshot(value)
    supplied_hash = _canonical_sha256(
        lock.get("monitoring_lock_sha256"), "monitoring_lock_sha256")
    unhashed = {key: item for key, item in lock.items()
                if key != "monitoring_lock_sha256"}
    if supplied_hash != _canonical_hash(unhashed):
        raise ValueError("monitoring_lock hash mismatch")
    fixed = {
        "schema_version": MONITORING_SCHEMA_VERSION,
        "protocol_id": MONITORING_PROTOCOL_ID,
        "selected_model": SELECTED_MODEL,
        "primary_arm": SELECTED_MODEL,
        "model_version": MODEL_VERSION,
        "feature_contract": FEATURE_CONTRACT,
        "engine_parameter_version": engine.PARAMETER_VERSION,
        "min_calibration_n": MIN_CALIBRATION_N,
        "nominal_joint_coverage": NOMINAL_JOINT_COVERAGE,
        "interval_method": INTERVAL_METHOD,
        "outcome_support": OUTCOME_SUPPORT,
        "randomization": None,
        "purpose": "forecast_monitoring_not_human_randomization",
    }
    if any(lock.get(key) != expected for key, expected in fixed.items()):
        raise ValueError("monitoring_lock does not match the fixed protocol preset")
    enrollment = lock.get("enrollment_id")
    if not isinstance(enrollment, str) or not enrollment or enrollment != enrollment.strip():
        raise ValueError("monitoring_lock enrollment_id is invalid")
    _canonical_sha256(lock.get("artifact_identity_sha256"),
                      "artifact_identity_sha256")
    try:
        registered = local_time(lock["registered_at"])
        future_start = local_time(lock["future_period_start"])
        calibration_end = local_time(lock["calibration_end"])
        heldout_end = local_time(lock["heldout_end"])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("monitoring_lock timestamps are invalid") from error
    for key, parsed in (
            ("registered_at", registered),
            ("future_period_start", future_start),
            ("calibration_end", calibration_end),
            ("heldout_end", heldout_end)):
        if lock.get(key) != parsed.isoformat():
            raise ValueError(f"monitoring_lock {key} is not canonical")
    if (future_start != _next_local_20_strictly_after(registered)
            or calibration_end != future_start + timedelta(days=CALIBRATION_DAYS)
            or heldout_end != calibration_end + timedelta(days=HELDOUT_DAYS)):
        raise ValueError("monitoring_lock chronology is invalid")
    return lock


def _monitoring_phase(lock, cutoff):
    cutoff = local_time(cutoff)
    start = local_time(lock["future_period_start"])
    calibration_end = local_time(lock["calibration_end"])
    heldout_end = local_time(lock["heldout_end"])
    if cutoff < start:
        return "pre_future_period"
    if cutoff < calibration_end:
        return "calibration"
    if cutoff < heldout_end:
        return "heldout"
    return "post_heldout"


def _validated_interval_calibration(value, lock):
    if not isinstance(value, dict):
        raise ValueError("interval_calibration must be an object")
    calibration = _snapshot(value)
    supplied_hash = _canonical_sha256(
        calibration.get("interval_calibration_sha256"),
        "interval_calibration_sha256")
    unhashed = {key: item for key, item in calibration.items()
                if key != "interval_calibration_sha256"}
    if supplied_hash != _canonical_hash(unhashed):
        raise ValueError("interval_calibration hash mismatch")
    expected = {
        "schema_version": INTERVAL_CALIBRATION_SCHEMA_VERSION,
        "artifact_type": "sleepwell_interval_calibration",
        "protocol_id": lock["protocol_id"],
        "monitoring_lock_sha256": lock["monitoring_lock_sha256"],
        "enrollment_id": lock["enrollment_id"],
        "model_version": lock["model_version"],
        "feature_contract": lock["feature_contract"],
        "engine_parameter_version": lock["engine_parameter_version"],
        "artifact_identity_sha256": lock["artifact_identity_sha256"],
        "primary_arm": lock["primary_arm"],
        "method": lock["interval_method"],
        "nominal_joint_coverage": lock["nominal_joint_coverage"],
        "min_calibration_n": lock["min_calibration_n"],
        "outcome_support": lock["outcome_support"],
        "calibration_period": {
            "start_inclusive": lock["future_period_start"],
            "end_exclusive": lock["calibration_end"],
        },
        "heldout_period": {
            "start_inclusive": lock["calibration_end"],
            "end_exclusive": lock["heldout_end"],
        },
        "terminal": True,
        "validated": False,
        "evidence_status": "calibration_only_not_validated",
    }
    if any(calibration.get(key) != expected_value
           for key, expected_value in expected.items()):
        raise ValueError("interval_calibration identity mismatch")
    try:
        frozen = local_time(calibration["frozen_at"])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("interval_calibration frozen_at is invalid") from error
    if calibration.get("frozen_at") != frozen.isoformat():
        raise ValueError("interval_calibration frozen_at is not canonical")
    if not (local_time(lock["calibration_end"]) <= frozen
            < local_time(lock["heldout_end"])):
        raise ValueError("interval_calibration was not frozen in time")
    count = calibration.get("calibration_n")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("interval_calibration calibration_n is invalid")
    rank = calibration.get("order_statistic_rank")
    radius = calibration.get("joint_radius_min")
    if count < lock["min_calibration_n"]:
        if (calibration.get("status") != "insufficient_calibration"
                or rank is not None or radius is not None):
            raise ValueError("insufficient interval_calibration is inconsistent")
    else:
        expected_rank = math.ceil((count + 1) * lock["nominal_joint_coverage"])
        if (calibration.get("status") != "calibrated_heldout_pending"
                or isinstance(rank, bool) or rank != expected_rank
                or isinstance(radius, bool) or not isinstance(radius, (int, float))
                or not math.isfinite(radius) or radius < 0):
            raise ValueError("calibrated interval_calibration is inconsistent")
    return calibration


def _empty_prediction_intervals(status, lock=None, calibration=None):
    return {
        "status": status,
        "method": lock.get("interval_method") if lock else None,
        "nominal_joint_coverage": (lock.get("nominal_joint_coverage")
                                    if lock else None),
        "calibration_n": (calibration.get("calibration_n")
                           if calibration else None),
        "arms": {},
        "interval_calibration_sha256": (
            calibration.get("interval_calibration_sha256")
            if calibration else None),
        "calibration_frozen_at": (calibration.get("frozen_at")
                                   if calibration else None),
        "validated": False,
    }


def _interval_bounds(cutoff, prediction, radius, support):
    onset = float(prediction["onset_offset_min"])
    wake = float(prediction["wake_offset_min"])
    if not all(math.isfinite(value) for value in (onset, wake, radius)):
        raise ValueError("prediction interval input is non-finite")
    onset_lower = max(float(support["onset_offset_min"]["lower"]), onset - radius)
    onset_upper = min(float(support["onset_offset_min"]["upper"]), onset + radius)
    wake_lower = max(float(support["wake_offset_min"]["lower"]), wake - radius)
    wake_upper = min(float(support["wake_offset_min"]["upper"]), wake + radius)
    return {
        "joint_radius_min": radius,
        "onset": {
            "lower": (cutoff + timedelta(minutes=onset_lower)).isoformat(),
            "upper": (cutoff + timedelta(minutes=onset_upper)).isoformat(),
            "width_min": onset_upper - onset_lower,
        },
        "wake": {
            "lower": (cutoff + timedelta(minutes=wake_lower)).isoformat(),
            "upper": (cutoff + timedelta(minutes=wake_upper)).isoformat(),
            "width_min": wake_upper - wake_lower,
        },
        "clipping": "declared_outcome_support_only",
    }


def _forecast_input_hash_valid(forecast):
    stored = forecast.get("input_hash")
    if not isinstance(stored, str):
        return False
    payload = {key: value for key, value in forecast.items()
               if key not in ("id", "forecast_id", "input_hash")}
    try:
        return stored == _canonical_hash(payload)
    except (TypeError, ValueError, OverflowError):
        return False


def _forecast_matches_lock(forecast, lock, *, require_input_hash=True, require_capture=True):
    try:
        if require_input_hash and not _forecast_input_hash_valid(forecast):
            return False
        if (forecast.get("model_version") != lock["model_version"]
                or forecast.get("feature_contract") != lock["feature_contract"]
                or forecast.get("selected_model") != lock["primary_arm"]):
            return False
        if ((forecast.get("profile_snapshot") or {}).get("enrollment_id") != lock["enrollment_id"]
                or (forecast.get("fusion_model") or {}).get("artifact_identity_sha256") != lock["artifact_identity_sha256"]
                or (forecast.get("parameter_snapshot") or {}).get("parameter_version") != lock["engine_parameter_version"]):
            return False
        if _validated_monitoring_lock(forecast.get("monitoring_lock_snapshot")) != lock:
            return False
        if forecast.get("monitoring_lock_sha256") != lock["monitoring_lock_sha256"]:
            return False
        cutoff = local_time(forecast["cutoff"])
        if local_time(forecast["generated_at"]) < local_time(lock["registered_at"]):
            return False
        if forecast.get("monitoring_phase") != _monitoring_phase(lock, cutoff):
            return False
        capture_ok, _reason = _capture_status(forecast)
        if require_capture and not capture_ok:
            return False
        expected_arms = _expected_arms(forecast)
        return expected_arms is not None and set(forecast.get("predictions", {})) == expected_arms
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _ordered_assessments(assessments):
    indexed = list(enumerate(assessments or []))
    def order(item):
        index, row = item
        identity = row.get("assessment_id") if isinstance(row, dict) else None
        if isinstance(identity, int) and not isinstance(identity, bool):
            return 0, identity, index
        return 1, index, index
    return [row for _index, row in sorted(indexed, key=order)]


def _fusion_snapshot(fusion_result, cutoff):
    """Validate and freeze the research-model output used at this cutoff."""
    if not isinstance(fusion_result, dict) or fusion_result.get("available") is not True:
        raise ValueError("fusion_result is unavailable")
    model_version = fusion_result.get("model_version")
    if not isinstance(model_version, str) or not model_version.strip():
        raise ValueError("fusion model_version is missing")
    if fusion_result.get("feature_contract") != FUSION_FEATURE_CONTRACT:
        raise ValueError("fusion feature_contract is unsupported")
    artifacts = fusion_result.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("fusion artifacts are missing")
    artifacts = _snapshot(artifacts)
    predictions = fusion_result.get("predictions")
    if not isinstance(predictions, dict) or set(predictions) != FUSION_ARMS:
        raise ValueError("fusion prediction arms are incomplete")
    frozen_predictions = {}
    for arm in sorted(FUSION_ARMS):
        prediction = predictions[arm]
        if not isinstance(prediction, dict):
            raise ValueError(f"fusion prediction {arm} is invalid")
        try:
            bed = local_time(prediction["bed"])
            wake = local_time(prediction["wake"])
            onset_offset = float(prediction["onset_offset_min"])
            wake_offset = float(prediction["wake_offset_min"])
            interval = float(prediction["interval_min"])
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"fusion prediction {arm} is invalid") from error
        if not all(math.isfinite(v) for v in (onset_offset, wake_offset, interval)):
            raise ValueError(f"fusion prediction {arm} contains a non-finite value")
        if not (0 <= onset_offset <= 960 and onset_offset + 30 <= wake_offset <= 1440
                and wake_offset - onset_offset <= 960):
            raise ValueError(f"fusion prediction {arm} is outside the target-night contract")
        if abs(interval - (wake_offset - onset_offset)) > 1e-6:
            raise ValueError(f"fusion prediction {arm} has an inconsistent interval")
        expected_bed = cutoff + timedelta(minutes=onset_offset)
        expected_wake = cutoff + timedelta(minutes=wake_offset)
        if abs((bed - expected_bed).total_seconds()) > 1 or abs((wake - expected_wake).total_seconds()) > 1:
            raise ValueError(f"fusion prediction {arm} does not match its cutoff offsets")
        frozen_predictions[arm] = {
            "bed": bed.isoformat(), "wake": wake.isoformat(),
            "onset_offset_min": onset_offset, "wake_offset_min": wake_offset,
            "interval_min": interval,
        }
    identity_input = {
        "model_version": model_version,
        "feature_contract": fusion_result["feature_contract"],
        "artifacts": artifacts,
    }
    metadata = {
        **identity_input,
        "artifact_identity_sha256": _canonical_hash(identity_input),
        "feature_snapshot": _snapshot(fusion_result.get("feature_snapshot", {})),
        "research_metrics": _snapshot(fusion_result.get("research_metrics", {})),
    }
    return frozen_predictions, metadata


def _expected_arms(forecast):
    """Return the exact registered arms for a recognized immutable contract."""
    contract = forecast.get("feature_contract")
    version = forecast.get("model_version")
    if contract == LEGACY_FEATURE_CONTRACT and version in (LEGACY_MODEL_VERSION, V2_MODEL_VERSION):
        return BASE_ARMS
    if (contract, version) not in (
            (V2_FEATURE_CONTRACT, V2_MODEL_VERSION),
            (V3_FEATURE_CONTRACT, V3_MODEL_VERSION),
            (FEATURE_CONTRACT, MODEL_VERSION)):
        return None
    metadata = forecast.get("fusion_model")
    if metadata is None:
        return BASE_ARMS
    if not isinstance(metadata, dict):
        return None
    identity_input = {key: metadata.get(key) for key in
                      ("model_version", "feature_contract", "artifacts")}
    if (not isinstance(identity_input["model_version"], str)
            or identity_input["feature_contract"] != FUSION_FEATURE_CONTRACT
            or not isinstance(identity_input["artifacts"], dict)
            or not identity_input["artifacts"]
            or metadata.get("artifact_identity_sha256") != _canonical_hash(identity_input)):
        return None
    return BASE_ARMS | FUSION_ARMS


def _profile_prediction(profile, cutoff):
    raw_latency = profile.get("latency_min")
    if raw_latency is None or raw_latency == "":
        raise ValueError("profile latency_min is required")
    latency = float(raw_latency)
    if not math.isfinite(latency) or not 0 <= latency <= 120:
        raise ValueError("profile latency_min is invalid")
    bed = _offset(_clock(profile["usual_bed"])) + latency
    wake = _offset(_clock(profile["usual_wake"]))
    if wake <= bed:
        wake += 1440
    return _iso_prediction(cutoff, bed, wake)


def _weighted_offset(history, field, cutoff, half_life_days=14.0):
    numerator = denominator = 0.0
    for row in history:
        age = max(0.0, (cutoff - row["wake"]).total_seconds() / 86400)
        weight = math.exp(-math.log(2) * age / half_life_days)
        numerator += weight * float(row[field])
        denominator += weight
    return numerator / denominator if denominator else None


def _adaptive_prediction(profile, history, params, cutoff, baseline):
    if not history or params.get("mode") != "personalized":
        return dict(baseline), {"mode": "no_usable_history", "history_count": len(history)}
    observed_bed = _weighted_offset(history, "bed_offset", cutoff)
    observed_wake = _weighted_offset(history, "wake_offset", cutoff)
    # engine.parameters already applies the declared 14-day half-life and turns
    # its effective record count into this shrinkage weight. Do not decay twice.
    last_age = max(0.0, (cutoff - history[-1]["wake"]).total_seconds() / 86400)
    reliability = params.get("recent_reliability", params.get("personalization_weight", 0.0))
    reliability = float(reliability or 0.0)
    reliability = min(1.0, max(0.0, reliability))
    parameter_bed = params.get("behavior_bed_offset", params.get("habit_bed_offset", observed_bed))
    parameter_wake = params.get("behavior_wake_offset", params.get("habit_wake_offset", observed_wake))
    bed_signal = float(parameter_bed if parameter_bed is not None else observed_bed)
    wake_signal = float(parameter_wake if parameter_wake is not None else observed_wake)
    bed = (1 - reliability) * baseline["onset_offset_min"] + reliability * bed_signal
    wake = (1 - reliability) * baseline["wake_offset_min"] + reliability * wake_signal
    prediction = _iso_prediction(cutoff, bed, wake)
    return prediction, {"mode": "recent_gap_aware", "history_count": len(history),
                        "latest_history_age_days": last_age, "reliability": reliability,
                        "observed_bed_offset": observed_bed, "observed_wake_offset": observed_wake}


def _schedule_offset(context, cutoff):
    candidates = []
    for index, item in enumerate(context.get("schedules", [])):
        try:
            start = local_time(item["start"])
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"Invalid schedules[{index}].start") from error
        # This arm predicts the main sleep following the cutoff. Same-evening
        # commitments are context, but they are not a following-morning wake cap.
        if start.date() == (cutoff + timedelta(days=1)).date():
            candidates.append((start - cutoff).total_seconds() / 60)
    for field in ("work_start", "class_start", "first_event", "first_schedule", "schedule_start"):
        value = context.get(field)
        if value not in (None, ""):
            try:
                candidates.append(_offset(_clock(value)))
            except (TypeError, ValueError):
                raise ValueError(f"Invalid {field}")
    return min(candidates) if candidates else None


def _context_prediction(adaptive, context, cutoff):
    earliest = _schedule_offset(context, cutoff)
    if earliest is None:
        prediction = dict(adaptive)
        return prediction, {"schedule_applied": False, "reason": "no_following_day_schedule",
                            "heuristic_unvalidated": True,
                            "commitment_overlap": _commitment_overlap(prediction, context, cutoff)}
    prep = float(context.get("preparation_min",
                             context.get("prep_min", context.get("schedule_prep_min", 60))) or 0)
    if not math.isfinite(prep) or not 0 <= prep <= 360:
        raise ValueError("prep_min must be between 0 and 360")
    wake_cap = earliest - prep
    duration = adaptive["interval_min"]
    wake = min(adaptive["wake_offset_min"], wake_cap)
    bed = wake - duration
    if bed < 0:
        bed = 0.0
    prediction = _iso_prediction(cutoff, bed, wake)
    return prediction, {"schedule_applied": True, "earliest_schedule_offset_min": earliest,
                        "prep_min": prep, "wake_cap_offset_min": wake_cap,
                        "heuristic_unvalidated": True,
                        "commitment_overlap": _commitment_overlap(prediction, context, cutoff)}


def _commitment_overlap(prediction, context, cutoff):
    sleep_start = cutoff + timedelta(minutes=prediction["onset_offset_min"])
    sleep_end = cutoff + timedelta(minutes=prediction["wake_offset_min"])
    overlaps = []
    for item in context.get("schedules", []):
        try:
            start = local_time(item["start"])
            end = local_time(item["end"]) if item.get("end") else start
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if start < sleep_end and end > sleep_start:
            overlaps.append({"kind": item.get("kind"), "start": start.isoformat(),
                             "end": end.isoformat()})
    return overlaps


def _capture_status(forecast):
    try:
        cutoff = local_time(forecast["cutoff"])
        generated = local_time(forecast["generated_at"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False, "invalid_capture_timestamp"
    if (cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond) != (20, 0, 0, 0):
        return False, "invalid_cutoff"
    if generated.date() != cutoff.date() or not cutoff <= generated <= cutoff + timedelta(minutes=15):
        return False, "late_generation"
    return True, "primary_window"


def _validate_context_snapshot(context, cutoff, enrollment_id=None):
    if not context:
        return
    if not context.get("observed_at"):
        raise ValueError("non-empty context_snapshot requires observed_at")
    if local_time(context["observed_at"]) >= cutoff:
        raise ValueError("context_snapshot must be available strictly before cutoff")
    if enrollment_id and context.get("enrollment_id") != enrollment_id:
        raise ValueError("context_snapshot enrollment identity mismatch")
    if context.get("night_id") != cutoff.date().isoformat():
        raise ValueError("context_snapshot night_id does not match cutoff")
    for index, nap in enumerate(context.get("naps", [])):
        try:
            start, end = local_time(nap["start"]), local_time(nap["end"])
            if not start < end:
                raise ValueError(f"naps[{index}] has an invalid interval")
            if end > cutoff:
                raise ValueError(f"naps[{index}] was not complete at cutoff")
        except (KeyError, TypeError, OverflowError) as error:
            raise ValueError(f"Invalid naps[{index}]") from error
    for index, dose in enumerate(context.get("caffeine", [])):
        try:
            if local_time(dose["at"]) > cutoff:
                raise ValueError(f"caffeine[{index}] was not known at cutoff")
        except (KeyError, TypeError, OverflowError) as error:
            raise ValueError(f"Invalid caffeine[{index}]") from error


def build_forecast(profile, record_versions, context_snapshot, cutoff, generated_at,
                   population_prior=None, fusion_result=None,
                   online_forecasts=None, online_assessments=None,
                   monitoring_lock=None, interval_calibration=None):
    """Build a timestamped prospective forecast from causal input snapshots."""
    cutoff = local_time(cutoff)
    generated_at = local_time(generated_at)
    if (cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond) != (20, 0, 0, 0):
        raise ValueError("cutoff must be exactly local 20:00")
    if generated_at < cutoff:
        raise ValueError("generated_at cannot precede cutoff or be backdated")
    eligible_capture, capture_reason = _capture_status(
        {"cutoff": cutoff.isoformat(), "generated_at": generated_at.isoformat()})

    profile_copy = _snapshot(profile)
    context_copy = _snapshot(context_snapshot or {})
    if local_time(profile_copy["created_at"]) >= cutoff:
        raise ValueError("profile must be created strictly before cutoff")
    _validate_context_snapshot(
        context_copy, cutoff, profile_copy.get("enrollment_id"))
    causal_records = _causal_diary_records(
        record_versions, cutoff, profile_copy["created_at"])
    history = normalize_history(
        known_onset_records(causal_records), cutoff, profile_copy["created_at"])
    params = _snapshot(engine.parameters(
        profile_copy, causal_records, cutoff, population_prior=population_prior))
    baseline = _profile_prediction(profile_copy, cutoff)
    adaptive, adaptive_features = _adaptive_prediction(
        profile_copy, history, params, cutoff, baseline)
    contextual, context_features = _context_prediction(
        adaptive, context_copy, cutoff)
    feature_snapshot = {
        "history_count": len(history),
        "history_latest_observed_at": (
            history[-1]["observed"].isoformat() if history else None),
        "adaptive": adaptive_features,
        "context": context_features,
        "evidence_quality": _snapshot(params.get("evidence_quality", {})),
        "nap_and_caffeine_policy": "captured_only_no_fitted_coefficient",
        "naps": context_copy.get("naps", context_copy.get("nap")),
        "caffeine": context_copy.get("caffeine"),
    }
    predictions = {"profile_baseline": baseline, "recent_adaptive": adaptive,
                   "context_candidate": contextual}
    fusion_model = None
    fusion_status = {"available": False, "reason": "not_provided"}
    if fusion_result is not None:
        try:
            fusion_predictions, fusion_model = _fusion_snapshot(fusion_result, cutoff)
            predictions.update(fusion_predictions)
            fusion_status = {
                "available": True,
                "artifact_identity_sha256": fusion_model["artifact_identity_sha256"],
            }
        except (TypeError, ValueError, OverflowError) as error:
            fusion_status = {
                "available": False,
                "reason": "rejected_invalid_fusion_result",
                "error_type": type(error).__name__,
            }
    feature_snapshot["fusion"] = fusion_status

    lock = None
    phase = "not_registered"
    calibration = None
    prediction_intervals = _empty_prediction_intervals("not_registered")
    if monitoring_lock is None:
        if interval_calibration is not None:
            raise ValueError("interval_calibration requires monitoring_lock")
    else:
        lock = _validated_monitoring_lock(monitoring_lock)
        if generated_at < local_time(lock["registered_at"]):
            raise ValueError("monitoring_lock cannot be applied retrospectively")
        if profile_copy.get("enrollment_id") != lock["enrollment_id"]:
            raise ValueError("monitoring_lock enrollment identity mismatch")
        if (fusion_model or {}).get("artifact_identity_sha256") != lock["artifact_identity_sha256"]:
            raise ValueError("monitoring_lock artifact identity mismatch")
        if params.get("parameter_version") != lock["engine_parameter_version"]:
            raise ValueError("monitoring_lock engine parameter version mismatch")
        phase = _monitoring_phase(lock, cutoff)
        if interval_calibration is not None:
            calibration = _validated_interval_calibration(interval_calibration, lock)
            if local_time(calibration["frozen_at"]) > generated_at:
                raise ValueError("interval_calibration was not frozen before forecast generation")
        if phase == "pre_future_period":
            prediction_intervals = _empty_prediction_intervals(
                "awaiting_future_data", lock, calibration)
        elif phase == "calibration":
            prediction_intervals = _empty_prediction_intervals(
                "calibration_in_progress", lock, calibration)
        elif phase == "heldout" and calibration is None:
            prediction_intervals = _empty_prediction_intervals(
                "awaiting_interval_calibration", lock)
        elif phase == "heldout" and calibration["status"] == "insufficient_calibration":
            prediction_intervals = _empty_prediction_intervals(
                "insufficient_calibration", lock, calibration)
        elif phase == "heldout":
            prediction_intervals = _empty_prediction_intervals(
                "calibrated_heldout_pending", lock, calibration)
            prediction_intervals["arms"] = {
                lock["primary_arm"]: _interval_bounds(
                    cutoff, predictions[lock["primary_arm"]],
                    float(calibration["joint_radius_min"]),
                    lock["outcome_support"]),
            }
        else:
            prediction_intervals = _empty_prediction_intervals(
                "heldout_period_complete", lock, calibration)

    artifact = {
        "night_id": cutoff.date().isoformat(),
        "cutoff": cutoff.isoformat(),
        "generated_at": generated_at.isoformat(),
        "eligible_capture": eligible_capture,
        "capture_reason": capture_reason,
        "model_version": MODEL_VERSION,
        "feature_contract": FEATURE_CONTRACT,
        "selected_model": SELECTED_MODEL,
        "policy_status": "unvalidated_prospective_behavior_forecast",
        "target": "main_sleep_onset_and_final_wake_not_bedtime_or_total_sleep_or_health_benefit",
        "profile_snapshot": profile_copy,
        "context_snapshot": context_copy,
        "parameter_snapshot": params,
        "feature_snapshot": feature_snapshot,
        "predictions": predictions,
        "monitoring_phase": phase,
        "prediction_intervals": prediction_intervals,
    }
    if fusion_model is not None:
        artifact["fusion_model"] = fusion_model
    if lock is not None:
        artifact["monitoring_lock_sha256"] = lock["monitoring_lock_sha256"]
        artifact["monitoring_lock_snapshot"] = lock
    if calibration is not None:
        artifact["interval_calibration_sha256"] = calibration["interval_calibration_sha256"]
    if fusion_model is not None and online_forecasts is not None:
        from online_trial import build_online_trial
        artifact["online_personalization"] = build_online_trial(
            artifact, online_forecasts, online_assessments or [])
    artifact["input_hash"] = _canonical_hash(artifact)
    return artifact


def fit_interval_calibration(lock, forecasts, assessments, frozen_at):
    """Freeze calibration-only residuals before the first held-out forecast."""
    locked = _validated_monitoring_lock(lock)
    frozen = local_time(frozen_at)
    calibration_end = local_time(locked["calibration_end"])
    heldout_end = local_time(locked["heldout_end"])
    if not (calibration_end <= frozen < heldout_end):
        raise ValueError("frozen_at must be at/after calibration_end and before heldout_end")

    matching = [forecast for forecast in forecasts or []
                if _forecast_matches_lock(forecast, locked)]
    heldout_generated = [local_time(forecast["generated_at"]) for forecast in forecasts or [] if _forecast_matches_lock(forecast, locked, require_capture=False) and _monitoring_phase(locked, forecast["cutoff"]) == "heldout"]
    if heldout_generated and frozen >= min(heldout_generated):
        raise ValueError("calibration must freeze before the first held-out forecast")
    calibration_forecasts = {
        forecast.get("id", forecast.get("forecast_id")): forecast
        for forecast in matching
        if _monitoring_phase(locked, forecast["cutoff"]) == "calibration"
        and forecast.get("id", forecast.get("forecast_id")) is not None
    }
    seen = set()
    residuals = []
    first_diary_n = 0
    exclusions = {
        "ineligible_first_report": 0,
        "label_not_available_before_calibration_end": 0,
        "incomplete_paired_endpoint": 0,
        "invalid_paired_endpoint": 0,
    }
    for assessment in _ordered_assessments(assessments):
        if not isinstance(assessment, dict):
            continue
        forecast_id = assessment.get("forecast_id")
        forecast = calibration_forecasts.get(forecast_id)
        if forecast is None or forecast_id in seen:
            continue
        outcome = assessment.get("outcome_snapshot") or {}
        reported = assessment.get("reported_record_snapshot") or {}
        source = outcome.get("source") or reported.get("source")
        if source != "diary":
            continue
        seen.add(forecast_id)
        first_diary_n += 1
        if not assessment.get("eligible"):
            exclusions["ineligible_first_report"] += 1
            continue
        if (assessment.get("monitoring_lock_sha256") != locked["monitoring_lock_sha256"]
                or assessment.get("model_version") != locked["model_version"]
                or assessment.get("feature_contract") != locked["feature_contract"]
                or assessment.get("enrollment_id") != locked["enrollment_id"]
                or assessment.get("artifact_identity_sha256")
                    != locked["artifact_identity_sha256"]
                or assessment.get("selected_model") != locked["primary_arm"]):
            exclusions["mixed_identity"] = exclusions.get("mixed_identity", 0) + 1
            continue
        try:
            observed = local_time(outcome["observed_at"])
        except (KeyError, TypeError, ValueError, OverflowError):
            exclusions["invalid_paired_endpoint"] += 1
            continue
        if observed >= calibration_end:
            exclusions["label_not_available_before_calibration_end"] += 1
            continue
        actual_onset = outcome.get("onset_offset_min")
        actual_wake = outcome.get("wake_offset_min")
        if actual_onset is None or actual_wake is None:
            exclusions["incomplete_paired_endpoint"] += 1
            continue
        try:
            actual_onset = float(actual_onset)
            actual_wake = float(actual_wake)
            onset_at = local_time(outcome["onset"])
            wake_at = local_time(outcome["wake"])
            generated = local_time(forecast["generated_at"])
            cutoff = local_time(forecast["cutoff"])
            prediction = forecast["predictions"][locked["primary_arm"]]
            predicted_onset = float(prediction["onset_offset_min"])
            predicted_wake = float(prediction["wake_offset_min"])
        except (KeyError, TypeError, ValueError, OverflowError):
            exclusions["invalid_paired_endpoint"] += 1
            continue
        values = (actual_onset, actual_wake, predicted_onset, predicted_wake)
        timestamp_offsets_match = (
            abs((onset_at - cutoff).total_seconds() / 60 - actual_onset) <= 1e-6
            and abs((wake_at - cutoff).total_seconds() / 60 - actual_wake) <= 1e-6)
        if (not all(math.isfinite(value) for value in values)
                or not generated < onset_at < wake_at <= observed
                or not timestamp_offsets_match):
            exclusions["invalid_paired_endpoint"] += 1
            continue
        residuals.append(max(abs(actual_onset - predicted_onset),
                             abs(actual_wake - predicted_wake)))

    count = len(residuals)
    if count >= locked["min_calibration_n"]:
        rank = math.ceil((count + 1) * locked["nominal_joint_coverage"])
        radius = sorted(residuals)[rank - 1]
        status = "calibrated_heldout_pending"
    else:
        rank = None
        radius = None
        status = "insufficient_calibration"
    artifact = {
        "schema_version": INTERVAL_CALIBRATION_SCHEMA_VERSION,
        "artifact_type": "sleepwell_interval_calibration",
        "protocol_id": locked["protocol_id"],
        "monitoring_lock_sha256": locked["monitoring_lock_sha256"],
        "enrollment_id": locked["enrollment_id"],
        "model_version": locked["model_version"],
        "feature_contract": locked["feature_contract"],
        "engine_parameter_version": locked["engine_parameter_version"],
        "artifact_identity_sha256": locked["artifact_identity_sha256"],
        "primary_arm": locked["primary_arm"],
        "frozen_at": frozen.isoformat(),
        "calibration_period": {
            "start_inclusive": locked["future_period_start"],
            "end_exclusive": locked["calibration_end"],
        },
        "heldout_period": {
            "start_inclusive": locked["calibration_end"],
            "end_exclusive": locked["heldout_end"],
        },
        "method": locked["interval_method"],
        "nominal_joint_coverage": locked["nominal_joint_coverage"],
        "min_calibration_n": locked["min_calibration_n"],
        "calibration_n": count,
        "order_statistic_rank": rank,
        "joint_radius_min": radius,
        "outcome_support": locked["outcome_support"],
        "candidate_calibration_forecast_n": len(calibration_forecasts),
        "first_diary_report_n": first_diary_n,
        "exclusions": exclusions,
        "status": status,
        "terminal": True,
        "validated": False,
        "evidence_status": "calibration_only_not_validated",
        "interpretation": (
            "Calibration residuals choose a fixed radius; held-out empirical coverage "
            "has not yet been observed and serial dependence is not removed."),
    }
    artifact["interval_calibration_sha256"] = _canonical_hash(artifact)
    return artifact


def _ineligible(forecast, reason):
    return {"forecast_id": forecast.get("id", forecast.get("forecast_id")), "night_id": forecast.get("night_id"),
            "model_version": forecast.get("model_version"), "generated_at": forecast.get("generated_at"),
            "selected_model": forecast.get("selected_model"), "eligible": False,
            "reason": reason, "errors": {}}


def evaluate_forecast(forecast, outcome):
    """Evaluate a stored point forecast without manufacturing a missing onset."""
    _finite(forecast, "forecast")
    _finite(outcome, "outcome")
    enrolled = (forecast.get("profile_snapshot") or {}).get("enrollment_id")
    if enrolled and outcome.get("enrollment_id") and enrolled != outcome["enrollment_id"]:
        return _ineligible(forecast, "different_enrollment")
    expected_arms = _expected_arms(forecast)
    if expected_arms is None:
        return _ineligible(forecast, "nonprospective_or_incompatible_forecast")
    eligible_capture, capture_reason = _capture_status(forecast)
    if not eligible_capture:
        return _ineligible(forecast, capture_reason)
    if (forecast.get("model_version"), forecast.get("feature_contract")) == (MODEL_VERSION, FEATURE_CONTRACT):
        if not _forecast_input_hash_valid(forecast):
            return _ineligible(forecast, "invalid_forecast_snapshot")
        lock_hash = forecast.get("monitoring_lock_sha256")
        if lock_hash is None:
            intervals = forecast.get("prediction_intervals") or {}
            if (forecast.get("monitoring_lock_snapshot") is not None
                    or forecast.get("monitoring_phase") != "not_registered"
                    or forecast.get("interval_calibration_sha256") is not None
                    or intervals.get("status") != "not_registered"):
                return _ineligible(forecast, "invalid_forecast_snapshot")
        else:
            try:
                lock = _validated_monitoring_lock(forecast.get("monitoring_lock_snapshot"))
            except (KeyError, TypeError, ValueError, OverflowError):
                return _ineligible(forecast, "invalid_forecast_snapshot")
            if not _forecast_matches_lock(forecast, lock):
                return _ineligible(forecast, "invalid_forecast_snapshot")
    try:
        generated = local_time(forecast["generated_at"])
        cutoff = local_time(forecast["cutoff"])
        if outcome.get("source") not in ("diary", "healthkit", "health_connect"):
            return _ineligible(forecast, "unsupported_outcome_source")
        observed = local_time(outcome["observed_at"])
        wake = local_time(outcome["wake"])
        onset, onset_provenance, latency = _recorded_onset(outcome)
    except (KeyError, TypeError, ValueError, OverflowError):
        return _ineligible(forecast, "invalid_outcome")
    if observed <= generated:
        return _ineligible(forecast, "outcome_not_observed_after_forecast")
    if observed < wake:
        return _ineligible(forecast, "outcome_observed_before_wake")

    if onset is None:
        try:
            bed = local_time(outcome["bed"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return _ineligible(forecast, "missing_onset_requires_bed_for_target_night")
        if bed < generated:
            return _ineligible(forecast, "unknown_onset_may_precede_forecast")
        if cutoff_for_onset(bed) != cutoff:
            return _ineligible(forecast, "different_target_night")
        bed_offset = (bed - cutoff).total_seconds() / 60
        wake_offset = (wake - cutoff).total_seconds() / 60
        bed_to_wake = (wake - bed).total_seconds() / 60
        if not (0 <= bed_offset <= 960 and 30 <= bed_to_wake <= 960
                and bed_offset + 30 <= wake_offset <= 1440):
            return _ineligible(forecast, "outcome_outside_contract")
        onset_offset = None
        interval = None
    else:
        if onset <= generated:
            return _ineligible(forecast, "sleep_started_before_forecast")
        if cutoff_for_onset(onset) != cutoff:
            return _ineligible(forecast, "different_target_night")
        onset_offset = (onset - cutoff).total_seconds() / 60
        wake_offset = (wake - cutoff).total_seconds() / 60
        interval = wake_offset - onset_offset
        if not (0 <= onset_offset <= 960
                and onset_offset + 30 <= wake_offset <= 1440
                and interval <= 960):
            return _ineligible(forecast, "outcome_outside_contract")

    predictions = forecast.get("predictions", {})
    if set(predictions) != expected_arms:
        return _ineligible(forecast, "incomplete_prediction_arms")
    errors = {}
    for arm, prediction in predictions.items():
        try:
            predicted_onset = float(prediction["onset_offset_min"])
            predicted_wake = float(prediction["wake_offset_min"])
            predicted_interval = float(prediction["interval_min"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return _ineligible(forecast, "invalid_prediction")
        if not all(math.isfinite(value) for value in (
                predicted_onset, predicted_wake, predicted_interval)):
            return _ineligible(forecast, "invalid_prediction")
        wake_error = abs(predicted_wake - wake_offset)
        if onset_offset is None:
            onset_error = interval_error = joint_error = None
        else:
            onset_error = abs(predicted_onset - onset_offset)
            interval_error = abs(predicted_interval - interval)
            joint_error = (onset_error + wake_error) / 2
        errors[arm] = {
            "onset_min": onset_error,
            "wake_min": wake_error,
            "interval_min": interval_error,
            "joint_min": joint_error,
        }
    if not errors:
        return _ineligible(forecast, "no_predictions")
    outcome_snapshot = {
        "source": outcome.get("source"),
        "observed_at": observed.isoformat(),
        "onset": onset.isoformat() if onset is not None else None,
        "wake": wake.isoformat(),
        "onset_offset_min": onset_offset,
        "wake_offset_min": wake_offset,
        "interval_min": interval,
        "onset_available": onset is not None,
        "onset_provenance": onset_provenance,
        "latency_min": latency,
        "interval_semantics": "main_sleep_onset_to_final_wake_not_total_sleep_time",
    }
    result = {
        "forecast_id": forecast.get("id", forecast.get("forecast_id")),
        "night_id": forecast.get("night_id"),
        "model_version": forecast.get("model_version"),
        "generated_at": forecast.get("generated_at"),
        "selected_model": forecast.get("selected_model"),
        "eligible": True,
        "reason": "eligible" if onset is not None else "eligible_wake_only",
        "endpoint_availability": {
            "onset": onset is not None,
            "wake": True,
            "interval": onset is not None,
            "joint": onset is not None,
        },
        "outcome_snapshot": outcome_snapshot,
        "errors": errors,
    }
    result["feature_contract"] = forecast.get("feature_contract")
    result["enrollment_id"] = enrolled
    result["artifact_identity_sha256"] = (
        (forecast.get("fusion_model") or {}).get("artifact_identity_sha256"))
    result["monitoring_lock_sha256"] = forecast.get("monitoring_lock_sha256")
    if "online_personalization" in forecast:
        from online_trial import evaluate_online_trial
        result["online_personalization"] = evaluate_online_trial(
            forecast, outcome_snapshot)
    return result


def summarize_evaluations(forecasts, evaluations):
    """Aggregate point-forecast accuracy with endpoint-specific denominators."""
    forecast_list = list(forecasts or [])
    evaluation_list = list(evaluations or [])
    by_id = {row.get("id"): row for row in forecast_list
             if row.get("id") is not None}
    by_compound = {(row.get("night_id"), row.get("generated_at")): row
                   for row in forecast_list}
    assessments = {}
    orphan_evaluations = 0
    duplicate_evaluations = 0
    for result in _ordered_assessments(evaluation_list):
        forecast = (by_id.get(result.get("forecast_id"))
                    if result.get("forecast_id") is not None else
                    by_compound.get((result.get("night_id"),
                                     result.get("generated_at"))))
        if forecast is None:
            orphan_evaluations += 1
            continue
        key = (("id", forecast["id"]) if forecast.get("id") is not None else
               ("compound", forecast.get("night_id"), forecast.get("generated_at")))
        if key in assessments:
            duplicate_evaluations += 1
            continue
        assessments[key] = result

    by_version = {}
    ineligible = {}
    primary_forecasts = 0
    pending = 0
    excluded = 0
    for forecast in forecast_list:
        key = (("id", forecast["id"]) if forecast.get("id") is not None else
               ("compound", forecast.get("night_id"), forecast.get("generated_at")))
        capture_ok, capture_reason = _capture_status(forecast)
        expected_arms = _expected_arms(forecast)
        if not capture_ok or expected_arms is None:
            excluded += 1
            reason = (capture_reason if not capture_ok else
                      "nonprospective_or_incompatible_forecast")
            ineligible[reason] = ineligible.get(reason, 0) + 1
            continue
        primary_forecasts += 1
        result = assessments.get(key)
        if result is None:
            pending += 1
            continue
        if not result.get("eligible"):
            reason = result.get("reason", "unknown")
            ineligible[reason] = ineligible.get(reason, 0) + 1
            excluded += 1
            continue
        if set(result.get("errors", {})) != expected_arms:
            ineligible["incomplete_evaluation_arms"] = (
                ineligible.get("incomplete_evaluation_arms", 0) + 1)
            excluded += 1
            continue
        version = forecast.get("model_version") or "unknown"
        fusion_identity = (forecast.get("fusion_model") or {}).get(
            "artifact_identity_sha256")
        version_key = (version, fusion_identity or "base_only")
        bucket = by_version.setdefault(version_key, {
            "model_version": version,
            "fusion_artifact_identity_sha256": fusion_identity,
            "eligible_count": 0,
            "arms": {},
            "selected_models": {},
            "source_strata": {},
        })
        bucket["eligible_count"] += 1
        selected = forecast.get("selected_model") or SELECTED_MODEL
        bucket["selected_models"][selected] = (
            bucket["selected_models"].get(selected, 0) + 1)
        for arm, errors in result["errors"].items():
            arm_bucket = bucket["arms"].setdefault(
                arm, {metric: [] for metric in ERROR_METRICS})
            for metric in ERROR_METRICS:
                value = errors.get(metric)
                if value is None:
                    continue
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError("Evaluation contains a non-finite error")
                arm_bucket[metric].append(value)
        source = (result.get("outcome_snapshot") or {}).get("source")
        stratum = ("user_reported_primary" if source == "diary" else
                   "automatic_wearable_auxiliary")
        source_bucket = bucket["source_strata"].setdefault(
            stratum, {"eligible_count": 0, "sources": {}, "arms": {}})
        source_bucket["eligible_count"] += 1
        source_bucket["sources"][source or "unknown"] = (
            source_bucket["sources"].get(source or "unknown", 0) + 1)
        for arm, errors in result["errors"].items():
            arm_bucket = source_bucket["arms"].setdefault(
                arm, {metric: [] for metric in ERROR_METRICS})
            for metric in ERROR_METRICS:
                value = errors.get(metric)
                if value is not None:
                    arm_bucket[metric].append(float(value))

    def aggregate_arms(arms):
        aggregated = {}
        for arm, values in arms.items():
            row = {}
            metric_counts = {}
            for metric, items in values.items():
                name = metric.removesuffix("_min")
                metric_counts[name] = len(items)
                row[f"{name}_n"] = len(items)
                row[f"{name}_mae_min"] = mean(items) if items else None
            row["n"] = row["joint_n"]
            row["metric_counts"] = metric_counts
            row["median_joint_min"] = (
                median(values["joint_min"]) if values["joint_min"] else None)
            aggregated[arm] = row
        return aggregated

    versions = []
    for bucket in by_version.values():
        bucket["arms"] = aggregate_arms(bucket["arms"])
        for source_bucket in bucket["source_strata"].values():
            source_bucket["arms"] = aggregate_arms(source_bucket["arms"])
        selected = SELECTED_MODEL
        selected_joint = (bucket["arms"].get(selected) or {}).get("joint_mae_min")
        profile_joint = (bucket["arms"].get("profile_baseline") or {}).get(
            "joint_mae_min")
        improvement = (profile_joint - selected_joint
                       if profile_joint is not None and selected_joint is not None
                       else None)
        version_row = {
            "model_version": bucket["model_version"],
            "fusion_artifact_identity_sha256":
                bucket["fusion_artifact_identity_sha256"],
            "n_nights": bucket["eligible_count"],
            "arms": bucket["arms"],
            "selected_models": bucket["selected_models"],
            "source_strata": bucket["source_strata"],
        }
        if improvement is not None:
            version_row["paired_selected_vs_profile_mae_improvement_min"] = improvement
        versions.append(version_row)
    versions.sort(key=lambda row: row["model_version"])
    eligible_total = sum(bucket["eligible_count"] for bucket in by_version.values())
    diary_total = sum(
        bucket["source_strata"].get("user_reported_primary", {}).get(
            "eligible_count", 0) for bucket in by_version.values())
    wearable_total = sum(
        bucket["source_strata"].get("automatic_wearable_auxiliary", {}).get(
            "eligible_count", 0) for bucket in by_version.values())
    return {
        "feature_contract": FEATURE_CONTRACT,
        "selected_model_preregistered": SELECTED_MODEL,
        "analysis_kind": "forecast_accuracy",
        "recommendation_effect": {
            "status": "not_estimated",
            "estimate": None,
            "reason": "No randomized or controlled human recommendation-effect design.",
        },
        "total_forecasts": len(forecast_list),
        "eligible_captures": primary_forecasts,
        "evaluated_nights": eligible_total,
        "pending_nights": pending,
        "excluded_nights": excluded,
        "ineligible_reasons": ineligible,
        "orphan_evaluations": orphan_evaluations,
        "duplicate_evaluations_ignored": duplicate_evaluations,
        "versions": versions,
        "explanation": (
            "20:00~20:15에 저장된 예측과 이후 처음 보고된 같은 밤 실제 결과만 비교합니다. "
            "입면이 없으면 기상 오차만 평가하고 입면·구간·결합 오차는 결측으로 유지합니다. "
            "사용자 일지는 주 평가 결과이고 자동 웨어러블 기록은 보조 지표입니다."),
        "outcome_source_policy": {
            "primary": "diary_user_reported",
            "auxiliary": ["healthkit", "health_connect"],
            "primary_evaluated_nights": diary_total,
            "auxiliary_evaluated_nights": wearable_total,
            "interpretation": "자동 웨어러블 결과는 착용·동기화 변수를 포함하므로 보조 지표로만 해석합니다.",
        },
        "calibration_progress": {
            "eligible_nights": eligible_total,
            "calibrated_intervals_available": False,
            "status": "use_separate_locked_interval_monitoring_contract",
        },
    }


def _stored_primary_interval(forecast, lock):
    intervals = forecast.get("prediction_intervals") or {}
    if (intervals.get("status") != "calibrated_heldout_pending"
            or intervals.get("method") != lock["interval_method"]
            or intervals.get("nominal_joint_coverage") != lock["nominal_joint_coverage"]
            or not isinstance(intervals.get("calibration_n"), int)
            or intervals["calibration_n"] < lock["min_calibration_n"]
            or set(intervals.get("arms", {})) != {lock["primary_arm"]}
            or intervals.get("interval_calibration_sha256")
                != forecast.get("interval_calibration_sha256")):
        return None
    arm = intervals["arms"][lock["primary_arm"]]
    cutoff = local_time(forecast["cutoff"])
    parsed = {}
    try:
        for endpoint in ("onset", "wake"):
            lower = local_time(arm[endpoint]["lower"])
            upper = local_time(arm[endpoint]["upper"])
            width = float(arm[endpoint]["width_min"])
            calculated_width = (upper - lower).total_seconds() / 60
            if (not math.isfinite(width) or lower > upper
                    or abs(width - calculated_width) > 1e-6):
                return None
            parsed[endpoint] = {
                "lower_offset_min": (lower - cutoff).total_seconds() / 60,
                "upper_offset_min": (upper - cutoff).total_seconds() / 60,
                "width_min": width,
            }
        onset_support = lock["outcome_support"]["onset_offset_min"]
        wake_support = lock["outcome_support"]["wake_offset_min"]
        if not (onset_support["lower"] <= parsed["onset"]["lower_offset_min"]
                <= parsed["onset"]["upper_offset_min"] <= onset_support["upper"]
                and wake_support["lower"] <= parsed["wake"]["lower_offset_min"]
                <= parsed["wake"]["upper_offset_min"] <= wake_support["upper"]):
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return parsed


def _coverage_metric(rows, endpoint):
    eligible = [row for row in rows if row.get(endpoint) is not None]
    covered = sum(bool(row[endpoint]) for row in eligible)
    return {
        "eligible_n": len(eligible),
        "covered_n": covered,
        "empirical_coverage": covered / len(eligible) if eligible else None,
        "mean_width_min": (mean(row[f"{endpoint}_width_min"] for row in eligible)
                           if eligible else None),
    }


def summarize_interval_monitoring(forecasts, assessments):
    """Describe held-out coverage from stored intervals without refitting them."""
    grouped = {}
    exclusions = {}
    for forecast in forecasts or []:
        if not isinstance(forecast, dict) or not forecast.get("monitoring_lock_sha256"):
            continue
        try:
            lock = _validated_monitoring_lock(
                forecast.get("monitoring_lock_snapshot"))
        except (TypeError, ValueError, OverflowError):
            exclusions["invalid_monitoring_lock"] = (
                exclusions.get("invalid_monitoring_lock", 0) + 1)
            continue
        if not _forecast_matches_lock(forecast, lock):
            exclusions["mixed_or_tampered_forecast_identity"] = (
                exclusions.get("mixed_or_tampered_forecast_identity", 0) + 1)
            continue
        phase = _monitoring_phase(lock, forecast["cutoff"])
        key = (lock["monitoring_lock_sha256"], lock["primary_arm"])
        group = grouped.setdefault(key, {
            "lock": lock,
            "forecasts": [],
            "calibration_hashes": set(),
            "interval_statuses": set(),
            "observed_phases": set(),
            "locked_forecast_n": 0,
        })
        group["observed_phases"].add(phase)
        group["locked_forecast_n"] += 1
        if phase != "heldout":
            continue
        group["forecasts"].append(forecast)
        intervals = forecast.get("prediction_intervals") or {}
        group["interval_statuses"].add(intervals.get("status", "missing"))
        calibration_hash = forecast.get("interval_calibration_sha256")
        if calibration_hash is not None:
            group["calibration_hashes"].add(calibration_hash)

    output_groups = []
    for (lock_hash, primary_arm), group in grouped.items():
        lock = group["lock"]
        base = {
            "monitoring_lock_sha256": lock_hash,
            "primary_arm": primary_arm,
            "model_version": lock["model_version"],
            "feature_contract": lock["feature_contract"],
            "engine_parameter_version": lock["engine_parameter_version"],
            "artifact_identity_sha256": lock["artifact_identity_sha256"],
            "heldout_period": {
                "start_inclusive": lock["calibration_end"],
                "end_exclusive": lock["heldout_end"],
            },
            "method": lock["interval_method"],
            "nominal_joint_coverage": lock["nominal_joint_coverage"],
            "nominal_coverage_is_guarantee": False,
            "validated": False,
        }
        if len(group["calibration_hashes"]) > 1:
            base.update({
                "status": "mixed_identity_refused",
                "interval_calibration_sha256": None,
                "heldout_forecast_n": len(group["forecasts"]),
                "issued_interval_n": 0,
                "issued_widths": {
                    "mean_onset_width_min": None,
                    "mean_wake_width_min": None,
                },
                "source_strata": {},
            })
            output_groups.append(base)
            continue
        calibration_hash = (next(iter(group["calibration_hashes"]))
                            if group["calibration_hashes"] else None)
        interval_forecasts = {}
        parsed_intervals = {}
        invalid_interval_n = 0
        for forecast in group["forecasts"]:
            parsed = _stored_primary_interval(forecast, lock)
            if parsed is None:
                if (forecast.get("prediction_intervals") or {}).get("arms"):
                    invalid_interval_n += 1
                continue
            forecast_id = forecast.get("id", forecast.get("forecast_id"))
            if forecast_id is None:
                invalid_interval_n += 1
                continue
            interval_forecasts[forecast_id] = forecast
            parsed_intervals[forecast_id] = parsed
        if invalid_interval_n:
            exclusions["invalid_stored_prediction_interval"] = (
                exclusions.get("invalid_stored_prediction_interval", 0)
                + invalid_interval_n)
        source_rows = {
            "user_reported_primary": [],
            "automatic_wearable_auxiliary": [],
        }
        seen = set()
        for assessment in _ordered_assessments(assessments):
            if not isinstance(assessment, dict):
                continue
            forecast_id = assessment.get("forecast_id")
            if forecast_id not in interval_forecasts:
                continue
            outcome = assessment.get("outcome_snapshot") or {}
            reported = assessment.get("reported_record_snapshot") or {}
            source = outcome.get("source") or reported.get("source")
            if source == "diary":
                stratum = "user_reported_primary"
            elif source in ("healthkit", "health_connect"):
                stratum = "automatic_wearable_auxiliary"
            else:
                continue
            first_key = (forecast_id, stratum)
            if first_key in seen:
                continue
            seen.add(first_key)
            if not assessment.get("eligible"):
                continue
            forecast = interval_forecasts[forecast_id]
            if (assessment.get("monitoring_lock_sha256") != lock["monitoring_lock_sha256"]
                    or assessment.get("model_version") != lock["model_version"]
                    or assessment.get("feature_contract") != lock["feature_contract"]
                    or assessment.get("enrollment_id") != lock["enrollment_id"]
                    or assessment.get("artifact_identity_sha256")
                        != lock["artifact_identity_sha256"]
                    or assessment.get("selected_model") != lock["primary_arm"]):
                exclusions["mixed_assessment_identity"] = (
                    exclusions.get("mixed_assessment_identity", 0) + 1)
                continue
            interval = parsed_intervals[forecast_id]
            try:
                wake = float(outcome["wake_offset_min"])
                if not math.isfinite(wake):
                    continue
                onset_raw = outcome.get("onset_offset_min")
                onset = None if onset_raw is None else float(onset_raw)
                if onset is not None and not math.isfinite(onset):
                    continue
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            onset_covered = (None if onset is None else
                interval["onset"]["lower_offset_min"] <= onset
                <= interval["onset"]["upper_offset_min"])
            wake_covered = (interval["wake"]["lower_offset_min"] <= wake
                            <= interval["wake"]["upper_offset_min"])
            source_rows[stratum].append({
                "onset": onset_covered,
                "wake": wake_covered,
                "joint": (None if onset_covered is None else
                          onset_covered and wake_covered),
                "onset_width_min": interval["onset"]["width_min"],
                "wake_width_min": interval["wake"]["width_min"],
            })
        source_strata = {}
        any_observation = False
        for stratum, rows in source_rows.items():
            onset_metric = _coverage_metric(rows, "onset")
            wake_metric = _coverage_metric(rows, "wake")
            joint_rows = [row for row in rows if row.get("joint") is not None]
            joint_covered = sum(bool(row["joint"]) for row in joint_rows)
            joint_metric = {
                "eligible_n": len(joint_rows),
                "covered_n": joint_covered,
                "empirical_coverage": (
                    joint_covered / len(joint_rows) if joint_rows else None),
                "mean_width_min": None,
                "width_semantics": "joint_rectangle_has_endpoint_specific_widths",
            }
            observed = bool(onset_metric["eligible_n"] or wake_metric["eligible_n"])
            any_observation = any_observation or observed
            source_strata[stratum] = {
                "status": ("heldout_coverage_observed" if observed else
                           "pending_future_observations"),
                "onset": onset_metric,
                "wake": wake_metric,
                "joint": joint_metric,
            }
        issued = list(parsed_intervals.values())
        if any_observation:
            status = "heldout_coverage_observed"
        elif "insufficient_calibration" in group["interval_statuses"]:
            status = "insufficient_calibration"
        else:
            status = "pending_future_observations"
        base.update({
            "status": status,
            "interval_calibration_sha256": calibration_hash,
            "heldout_forecast_n": len(group["forecasts"]),
            "issued_interval_n": len(issued),
            "issued_widths": {
                "mean_onset_width_min": (
                    mean(row["onset"]["width_min"] for row in issued)
                    if issued else None),
                "mean_wake_width_min": (
                    mean(row["wake"]["width_min"] for row in issued)
                    if issued else None),
            },
            "source_strata": source_strata,
        })
        output_groups.append(base)
    output_groups.sort(key=lambda row: (
        row["monitoring_lock_sha256"], row["primary_arm"]))
    return {
        "schema_version": 1,
        "analysis_kind": "forecast_accuracy",
        "groups": output_groups,
        "excluded_reasons": exclusions,
        "recommendation_effect": {
            "status": "not_estimated",
            "estimate": None,
            "reason": "Forecast monitoring is not a randomized or controlled effect study.",
        },
        "interpretation": (
            "Held-out empirical coverage is descriptive under serial dependence. "
            "Calibration alone is not interval validation and nominal coverage is not guaranteed."),
    }
