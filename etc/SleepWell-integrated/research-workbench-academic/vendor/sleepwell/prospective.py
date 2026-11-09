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


MODEL_VERSION = "prospective-behavior-v3-daily-personalization"
FEATURE_CONTRACT = "sleepwell_prospective_pre20_v3_daily_personalization"
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
EXPECTED_ARMS = BASE_ARMS


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
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    # Forecast onset, not time into bed: declared latency is applied explicitly.
    bed = _offset(_clock(profile["usual_bed"])) + float(profile.get("latency_min") or 0)
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


def _validate_context_snapshot(context, cutoff):
    if not context:
        return
    if not context.get("observed_at"):
        raise ValueError("non-empty context_snapshot requires observed_at")
    if local_time(context["observed_at"]) > cutoff:
        raise ValueError("context_snapshot was not available at cutoff")
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
                   online_forecasts=None, online_assessments=None):
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
    if local_time(profile_copy["created_at"]) > cutoff:
        raise ValueError("profile was not created at cutoff")
    _validate_context_snapshot(context_copy, cutoff)
    # Strict prefilter protects against engine compatibility paths that accept
    # records without an availability timestamp.
    history = normalize_history(record_versions or [], cutoff, profile_copy.get("created_at"))
    causal_records = [{"bed": row["bed"].isoformat(), "wake": row["wake"].isoformat(),
                       "sleep_start": row["bed"].isoformat(), "observed_at": row["observed"].isoformat(),
                       "source": "diary", "sleep_min": row["duration"]} for row in history]
    try:
        params = engine.parameters(profile_copy, causal_records, cutoff,
                                   population_prior=population_prior)
    except TypeError:
        params = engine.parameters(profile_copy, causal_records, cutoff, None, population_prior)
    params = _snapshot(params)
    baseline = _profile_prediction(profile_copy, cutoff)
    adaptive, adaptive_features = _adaptive_prediction(profile_copy, history, params, cutoff, baseline)
    contextual, context_features = _context_prediction(adaptive, context_copy, cutoff)
    feature_snapshot = {
        "history_count": len(history),
        "history_latest_observed_at": history[-1]["observed"].isoformat() if history else None,
        "adaptive": adaptive_features,
        "context": context_features,
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
            fusion_status = {"available": True,
                             "artifact_identity_sha256": fusion_model["artifact_identity_sha256"]}
        except (TypeError, ValueError, OverflowError) as error:
            # Scheduled capture must survive a missing/corrupt research artifact.
            # The bounded reason contains no model payload or user values.
            fusion_status = {"available": False, "reason": "rejected_invalid_fusion_result",
                             "error_type": type(error).__name__}
    feature_snapshot["fusion"] = fusion_status
    artifact = {
        "night_id": cutoff.date().isoformat(),
        "cutoff": cutoff.isoformat(),
        "generated_at": generated_at.isoformat(),
        "eligible_capture": eligible_capture,
        "capture_reason": capture_reason,
        # Every newly generated forecast carries the daily-personalization wrapper contract.
        # Frozen fusion artifacts retain their own identity inside fusion_model.
        "model_version": MODEL_VERSION,
        "feature_contract": FEATURE_CONTRACT,
        "selected_model": SELECTED_MODEL,
        "policy_status": "unvalidated_prospective_behavior_forecast",
        "target": "main_sleep_onset_and_final_wake_not_bedtime_or_health_benefit",
        "profile_snapshot": profile_copy,
        "context_snapshot": context_copy,
        "parameter_snapshot": params,
        "feature_snapshot": feature_snapshot,
        "predictions": predictions,
    }
    if fusion_model is not None:
        artifact["fusion_model"] = fusion_model
        if online_forecasts is not None:
            from online_trial import build_online_trial
            artifact["online_personalization"] = build_online_trial(
                artifact, online_forecasts, online_assessments or [])
    artifact["input_hash"] = _canonical_hash(artifact)
    return artifact


def _ineligible(forecast, reason):
    return {"forecast_id": forecast.get("id", forecast.get("forecast_id")), "night_id": forecast.get("night_id"),
            "model_version": forecast.get("model_version"), "generated_at": forecast.get("generated_at"),
            "selected_model": forecast.get("selected_model"), "eligible": False,
            "reason": reason, "errors": {}}


def evaluate_forecast(forecast, outcome):
    """Evaluate one saved forecast against one eligible, subsequently observed outcome."""
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
    try:
        generated = local_time(forecast["generated_at"])
        cutoff = local_time(forecast["cutoff"])
        if outcome.get("source") not in ("diary", "healthkit", "health_connect"):
            return _ineligible(forecast, "unsupported_outcome_source")
        observed = local_time(outcome["observed_at"])
        if observed <= generated:
            return _ineligible(forecast, "outcome_not_observed_after_forecast")
        onset = local_time(outcome.get("sleep_start") or outcome["bed"])
        if not outcome.get("sleep_start"):
            onset += timedelta(minutes=float(outcome.get("latency_min") or 0))
        wake = local_time(outcome["wake"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return _ineligible(forecast, "invalid_outcome")
    if onset <= generated:
        return _ineligible(forecast, "sleep_started_before_forecast")
    if observed < wake:
        return _ineligible(forecast, "outcome_observed_before_wake")
    if cutoff_for_onset(onset) != cutoff:
        return _ineligible(forecast, "different_target_night")
    onset_offset = (onset - cutoff).total_seconds() / 60
    wake_offset = (wake - cutoff).total_seconds() / 60
    duration = wake_offset - onset_offset
    if not (0 <= onset_offset <= 960 and onset_offset + 30 <= wake_offset <= 1440 and duration <= 960):
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
        except (KeyError, TypeError, ValueError):
            return _ineligible(forecast, "invalid_prediction")
        if not all(math.isfinite(v) for v in (predicted_onset, predicted_wake, predicted_interval)):
            return _ineligible(forecast, "invalid_prediction")
        onset_error = abs(predicted_onset - onset_offset)
        wake_error = abs(predicted_wake - wake_offset)
        errors[arm] = {"onset_min": onset_error, "wake_min": wake_error,
                       "interval_min": abs(predicted_interval - duration),
                       "joint_min": (onset_error + wake_error) / 2}
    if not errors:
        return _ineligible(forecast, "no_predictions")
    outcome_snapshot = {"source": outcome.get("source"), "observed_at": observed.isoformat(),
                        "onset": onset.isoformat(), "wake": wake.isoformat(),
                        "onset_offset_min": onset_offset, "wake_offset_min": wake_offset,
                        "interval_min": duration}
    result = {"forecast_id": forecast.get("id", forecast.get("forecast_id")), "night_id": forecast.get("night_id"),
            "model_version": forecast.get("model_version"), "generated_at": forecast.get("generated_at"),
            "selected_model": forecast.get("selected_model"), "eligible": True, "reason": "eligible",
            "outcome_snapshot": outcome_snapshot, "errors": errors}
    if "online_personalization" in forecast:
        from online_trial import evaluate_online_trial
        result["online_personalization"] = evaluate_online_trial(forecast, outcome_snapshot)
    return result


def summarize_evaluations(forecasts, evaluations):
    """Aggregate only eligible prospectively captured evaluations by model version."""
    forecast_list = list(forecasts or [])
    evaluation_list = list(evaluations or [])
    by_id = {row.get("id"): row for row in forecast_list if row.get("id") is not None}
    by_compound = {(row.get("night_id"), row.get("generated_at")): row
                   for row in forecast_list}
    assessments = {}
    orphan_evaluations = 0
    duplicate_evaluations = 0
    for result in evaluation_list:
        forecast = None
        if result.get("forecast_id") is not None:
            forecast = by_id.get(result["forecast_id"])
        else:
            forecast = by_compound.get((result.get("night_id"), result.get("generated_at")))
        if forecast is None:
            orphan_evaluations += 1
            continue
        key = ("id", forecast["id"]) if forecast.get("id") is not None else (
            "compound", forecast.get("night_id"), forecast.get("generated_at"))
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
            reason = capture_reason if not capture_ok else "nonprospective_or_incompatible_forecast"
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
            ineligible["incomplete_evaluation_arms"] = ineligible.get("incomplete_evaluation_arms", 0) + 1
            excluded += 1
            continue
        version = forecast.get("model_version") or "unknown"
        fusion_identity = (forecast.get("fusion_model") or {}).get("artifact_identity_sha256")
        version_key = (version, fusion_identity or "base_only")
        bucket = by_version.setdefault(version_key, {"model_version": version,
                                                     "fusion_artifact_identity_sha256": fusion_identity,
                                                     "eligible_count": 0, "arms": {},
                                                     "selected_models": {}, "source_strata": {}})
        bucket["eligible_count"] += 1
        selected = forecast.get("selected_model") or SELECTED_MODEL
        bucket["selected_models"][selected] = bucket["selected_models"].get(selected, 0) + 1
        for arm, errors in result.get("errors", {}).items():
            arm_bucket = bucket["arms"].setdefault(arm, {key: [] for key in ("onset_min", "wake_min", "interval_min", "joint_min")})
            for key in arm_bucket:
                value = float(errors[key])
                if not math.isfinite(value):
                    raise ValueError("Evaluation contains a non-finite error")
                arm_bucket[key].append(value)
        source = (result.get("outcome_snapshot") or {}).get("source")
        stratum = ("user_reported_primary" if source == "diary"
                   else "automatic_wearable_auxiliary")
        source_bucket = bucket["source_strata"].setdefault(
            stratum, {"eligible_count": 0, "sources": {}, "arms": {}})
        source_bucket["eligible_count"] += 1
        source_bucket["sources"][source or "unknown"] = (
            source_bucket["sources"].get(source or "unknown", 0) + 1)
        for arm, errors in result.get("errors", {}).items():
            arm_bucket = source_bucket["arms"].setdefault(
                arm, {key: [] for key in ("onset_min", "wake_min", "interval_min", "joint_min")})
            for metric in arm_bucket:
                arm_bucket[metric].append(float(errors[metric]))
    versions = []
    def aggregate_arms(arms):
        result = {}
        for arm, values in arms.items():
            result[arm] = {"n": len(values["joint_min"]),
                           **{f"{key.replace('_min', '')}_mae_min": mean(items)
                              for key, items in values.items()},
                           "median_joint_min": median(values["joint_min"])}
        return result

    for _version_key, bucket in by_version.items():
        version = bucket["model_version"]
        bucket["arms"] = aggregate_arms(bucket["arms"])
        for source_bucket in bucket["source_strata"].values():
            source_bucket["arms"] = aggregate_arms(source_bucket["arms"])
        selected = SELECTED_MODEL
        if selected in bucket["arms"] and "profile_baseline" in bucket["arms"]:
            bucket["paired_selected_vs_profile_mae_improvement_min"] = (
                bucket["arms"]["profile_baseline"]["joint_mae_min"] -
                bucket["arms"][selected]["joint_mae_min"])
        versions.append({"model_version": version,
                         "fusion_artifact_identity_sha256": bucket["fusion_artifact_identity_sha256"],
                         "n_nights": bucket["eligible_count"],
                         "arms": bucket["arms"], "selected_models": bucket["selected_models"],
                         "source_strata": bucket["source_strata"],
                         **({"paired_selected_vs_profile_mae_improvement_min":
                             bucket["paired_selected_vs_profile_mae_improvement_min"]}
                            if "paired_selected_vs_profile_mae_improvement_min" in bucket else {})})
    versions.sort(key=lambda row: row["model_version"])
    eligible_total = sum(bucket["eligible_count"] for bucket in by_version.values())
    diary_total = sum(bucket["source_strata"].get("user_reported_primary", {}).get("eligible_count", 0)
                      for bucket in by_version.values())
    wearable_total = sum(bucket["source_strata"].get("automatic_wearable_auxiliary", {}).get("eligible_count", 0)
                         for bucket in by_version.values())
    return {"feature_contract": FEATURE_CONTRACT, "selected_model_preregistered": SELECTED_MODEL,
            "total_forecasts": len(forecast_list), "eligible_captures": primary_forecasts,
            "evaluated_nights": eligible_total,
            "pending_nights": pending, "excluded_nights": excluded,
            "ineligible_reasons": ineligible, "orphan_evaluations": orphan_evaluations,
            "duplicate_evaluations_ignored": duplicate_evaluations, "versions": versions,
            "explanation": ("20:00~20:15에 저장된 예측과 이후 처음 보고된 같은 밤 실제 결과만 비교합니다. "
                            "사용자 일지는 주 평가 결과이고 자동 웨어러블 기록은 착용·동기화 영향을 받는 보조 지표입니다. "
                            "최근 적응 예측은 등록된 비교 기준이며 융합 예측의 우승을 가정하지 않습니다."),
            "outcome_source_policy": {
                "primary": "diary_user_reported", "auxiliary": ["healthkit", "health_connect"],
                "primary_evaluated_nights": diary_total,
                "auxiliary_evaluated_nights": wearable_total,
                "interpretation": "자동 웨어러블 결과는 착용·동기화 변수를 포함하므로 보조 지표로만 해석합니다.",
            },
            "calibration_progress": {"eligible_nights": eligible_total,
                                     "minimum_initial_review_nights": 14,
                                     "initial_review_ready": eligible_total >= 14,
                                     "calibrated_intervals_available": False,
                                     "status": "descriptive_prospective_monitoring_only"}}
