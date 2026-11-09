"""Read-only diagnostics for aggregate research models and phone snapshots."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
import json
import math
from pathlib import Path
import re

from mobile_backup import snapshots as mobile_snapshots, summary as mobile_summary
from fusion_forecast import FusionModelError, PortableCohortModel, _pipeline_predict
from night_core import constrain
from robust_features import HYBRID_V2


SCHEMA_VERSION = 1
MOBILE_LIMIT = 365
PARTICIPANT_KEY = re.compile(r"[0-9a-f]{32}")
MODEL_FILES = {"cmi": "robust_cmi_v2.json", "mesa": "robust_mesa_v2.json"}
DIARY_SOURCES = {"manual", "diary", "user_entry"}
WEARABLE_SOURCES = {"samsung_health", "health_connect", "healthkit"}
REPLAY_TOLERANCE_MIN = 1e-6
REPLAY_SCOPE = (
    "stored features to inference only; does not validate feature derivation, physical trajectory, "
    "frozen capture, predictive accuracy"
)


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _mapping(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field}_not_object")
    return value


def _aware(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field}_not_timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError(f"{field}_missing_timezone")
    return value


def _day(value, field="day"):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError(f"{field}_invalid")
    return value


def _number(value, field):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{field}_not_finite")
    return value


def _source_role(source):
    normalized = source.strip().lower()
    if normalized in DIARY_SOURCES:
        return "diary_primary"
    if normalized in WEARABLE_SOURCES:
        return "wearable_auxiliary"
    return "other"


def _safe_fields(value, fields):
    if not isinstance(value, dict):
        return None
    return _json_copy({key: value[key] for key in fields if key in value})


def _load_artifact(path, cohort):
    validated = PortableCohortModel(path, cohort)
    raw = validated.raw
    metrics = _mapping(raw["research_metrics"], "research_metrics")
    test = _mapping(metrics.get("test"), "test_metrics")
    if set(test) != {"physical", "data", "hybrid"}:
        raise ValueError("missing_model_arm_metrics")
    for arm, values in test.items():
        values = _mapping(values, f"{arm}_metrics")
        for key in ("joint_mae_min", "onset_mae_min", "wake_mae_min", "interval_mae_min"):
            _number(values.get(key), f"{arm}_{key}")
    return {
        "cohort": cohort,
        "artifact_sha256": validated.sha256,
        "model_version": raw["model_version"],
        "status": raw["status"],
        "target": raw["target"],
        "decision_time": raw["decision_time"],
        "feature_contract": raw["feature_contract"],
        "physical_provider": raw["physical_provider"],
        "prior": _json_copy(raw["prior"]),
        "provenance": _json_copy(raw["provenance"]),
        "test_metrics": _json_copy(test),
        "parity": _json_copy(metrics.get("parity", {})),
        "preprocessing": {
            "processing": metrics.get("processing"),
            "selection": metrics.get("selection"),
        },
        "test_caveat": metrics.get("test_status"),
        "limitations": _json_copy(raw["limitations"]),
    }


def _load_population_prior(path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(raw, dict) or raw.get("artifact_type") != "sleepwell_population_prior"
                or not isinstance(raw.get("groups"), list)):
            raise ValueError("invalid_population_prior")
        return {
            "available": True,
            "version": raw.get("version"),
            "source": raw.get("source"),
            "source_sha256": raw.get("source_sha256"),
            "supported_age": _json_copy(raw.get("supported_age")),
            "group_count": len(raw["groups"]),
            "minimum_group_people": raw.get("minimum_group_people"),
            "interpretation": raw.get("interpretation"),
            "unsupported_fields": _json_copy(raw.get("unsupported_fields")),
            "global": _json_copy(raw.get("global")),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return {"available": False, "reason": type(error).__name__}


def research_diagnostics(models_dir):
    models_dir = Path(models_dir)
    cohorts, errors = [], []
    for cohort, filename in MODEL_FILES.items():
        try:
            cohorts.append(_load_artifact(models_dir / filename, cohort))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            errors.append({"cohort": cohort, "status": "unavailable", "reason": type(error).__name__})
    return {
        "status": "available" if len(cohorts) == len(MODEL_FILES) else "incomplete",
        "caveat": (
            "CMI·MESA Test 결과는 기존 Validation/Test를 재사용한 적응적 분석입니다. "
            "새 외부 검증이나 참가자 개인 정확도로 해석할 수 없습니다."
        ),
        "cohorts": cohorts,
        "artifact_errors": errors,
        "population_prior": _load_population_prior(models_dir / "population_prior.json"),
        "methodology": {
            "physical_model": {
                "provider": "two_process_fixed_v1",
                "fixed_parameters": {"tau_wake_h": 18.2, "tau_sleep_h": 4.2, "circadian_amplitude": 0.15,
                                     "simulation_step_min": 5},
                "equations": {
                    "awake": "S(t)=1-(1-S0)*exp(-t/18.2)",
                    "asleep": "S(t)=S0*exp(-t/4.2)",
                    "circadian": "C(t)=0.15*cos(2*pi*(local_minute-phase)/1440)",
                    "transition_drive": "D(t)=S(t)+C(t); onset at D>=sleep_threshold, wake at D<=wake_threshold",
                },
                "interpretation": "고정 시간상수의 공학적 Two-Process 기준선이며 측정된 개인 생리값이 아닙니다.",
            },
            "frozen_model_personalization": {
                "activation": "valid_days>=14 and elapsed_days>=14",
                "weight": "w=n/(n+14), otherwise 0",
                "habit": "habit=(1-w)*training_prior+w*mean(valid_person_history)",
                "note": "연구 모델의 동결된 특징 계약입니다. 앱 권장시간의 최근성 감쇠 개인화와 별도입니다.",
            },
            "model_arms": {
                "physical": "Two-Process 계산만 사용",
                "data": "인과적 과거 수면 특징을 사용하는 학습 모델",
                "hybrid": "동일 데이터 특징에 Two-Process 상태·예측 특징을 추가한 학습 모델",
            },
        },
    }


def _validate_record(record):
    record = _mapping(record, "record")
    required = {"id", "day", "bedtime", "wake_time", "duration_minutes", "source", "modified_at"}
    if not required.issubset(record):
        raise ValueError("record_missing_fields")
    if not isinstance(record["id"], str) or not record["id"]:
        raise ValueError("record_id_invalid")
    _day(record["day"], "record_day")
    _aware(record["bedtime"], "record_bedtime")
    _aware(record["wake_time"], "record_wake_time")
    _aware(record["modified_at"], "record_modified_at")
    if type(record["duration_minutes"]) is not int or not 0 <= record["duration_minutes"] <= 1440:
        raise ValueError("record_duration_invalid")
    if not isinstance(record["source"], str) or not 1 <= len(record["source"]) <= 64:
        raise ValueError("record_source_invalid")
    return record


def _record_summary(records):
    valid = [_validate_record(record) for record in records]
    counts = Counter(record["source"] for record in valid)
    roles = Counter(_source_role(record["source"]) for record in valid)
    latest = max((record["day"] for record in valid), default=None)
    return {
        "count": len(valid),
        "source_counts": dict(sorted(counts.items())),
        "source_roles": dict(sorted(roles.items())),
        "latest_date": latest,
    }, valid


def _record_trend(records):
    ordered = sorted(records, key=lambda item: (item["day"], item["modified_at"]))[-90:]
    return [{
        "day": item["day"],
        "bedtime": item["bedtime"],
        "wake_time": item["wake_time"],
        "duration_minutes": item["duration_minutes"],
        "rested": item.get("rested"),
        "fatigue": item.get("fatigue"),
        "source": item["source"],
        "source_role": _source_role(item["source"]),
    } for item in ordered]


def _forecast_summary(value):
    return _safe_fields(value, (
        "generated_at", "cmi_bed_offset", "mesa_bed_offset", "ensemble_bed_offset", "note"
    ))


def _recommendation_summary(value):
    return _safe_fields(value, (
        "generated_at", "wake_time", "bedtime", "duration_minutes", "regularity_minutes",
        "subjective_quality", "process_s", "plan_feasible", "explanation",
    ))


def _unavailable_replay(reason):
    return {
        "status": "unavailable", "scope": REPLAY_SCOPE,
        "tolerance_min": REPLAY_TOLERANCE_MIN, "comparisons": {}, "reason": reason,
    }


def replay_feature_outputs(diag, models_dir):
    """Check stored feature-to-output arithmetic against exact installed artifacts.

    This deliberately starts after feature derivation. It is not an end-to-end
    reconstruction of the phone forecast and cannot establish predictive accuracy.
    """
    if not isinstance(diag, dict):
        return _unavailable_replay("phone_model_diagnostics_missing")
    artifacts = diag.get("research_artifacts")
    features = diag.get("research_feature_snapshot")
    reported = diag.get("research_predictions")
    if not all(isinstance(value, dict) for value in (artifacts, features, reported)):
        return _unavailable_replay("stored_artifacts_features_or_outputs_missing")

    models = {}
    try:
        for cohort, filename in MODEL_FILES.items():
            model = PortableCohortModel(Path(models_dir) / filename, cohort)
            metadata = artifacts.get(cohort)
            values = features.get(cohort)
            if not isinstance(metadata, dict) or not isinstance(values, dict):
                return _unavailable_replay(f"{cohort}_metadata_or_features_missing")
            if (metadata.get("cohort") != cohort or metadata.get("sha256") != model.sha256
                    or metadata.get("model_version") != model.raw["model_version"]
                    or metadata.get("feature_contract") != model.raw["feature_contract"]
                    or metadata.get("physical_provider") != model.raw["physical_provider"]
                    or metadata.get("source_model_sha256") != model.raw["source_model_sha256"]
                    or diag.get("model_version") != model.raw["model_version"]
                    or diag.get("model_feature_contract") != model.raw["feature_contract"]
                    or diag.get("physical_provider") != model.raw["physical_provider"]):
                return _unavailable_replay(f"{cohort}_artifact_contract_mismatch")
            if set(values) != set(HYBRID_V2):
                return _unavailable_replay(f"{cohort}_feature_vector_contract_mismatch")
            if any(type(value) not in (int, float) or not math.isfinite(value)
                   for value in values.values()):
                return _unavailable_replay(f"{cohort}_feature_vector_not_finite")
            models[cohort] = (model, values)
    except FusionModelError:
        return _unavailable_replay("installed_artifact_validation_failed")

    comparisons = {}
    try:
        for cohort, (model, values) in models.items():
            expected_by_arm = {
                "physical": constrain(values["physical_bed"], values["physical_wake"]),
                "data": _pipeline_predict(model.raw["models"]["data"], values),
                "hybrid": _pipeline_predict(model.raw["models"]["hybrid"], values),
            }
            for arm, expected in expected_by_arm.items():
                key = f"{arm}_{cohort}"
                output = reported.get(key)
                if not isinstance(output, dict):
                    return _unavailable_replay(f"{key}_reported_output_missing")
                onset = output.get("onset_offset_min")
                wake = output.get("wake_offset_min")
                if (type(onset) not in (int, float) or not math.isfinite(onset)
                        or type(wake) not in (int, float) or not math.isfinite(wake)):
                    return _unavailable_replay(f"{key}_reported_output_invalid")
                onset_delta = abs(float(expected[0]) - float(onset))
                wake_delta = abs(float(expected[1]) - float(wake))
                matches = onset_delta <= REPLAY_TOLERANCE_MIN and wake_delta <= REPLAY_TOLERANCE_MIN
                comparisons[key] = {
                    "expected": {"onset_offset_min": expected[0], "wake_offset_min": expected[1]},
                    "reported": {"onset_offset_min": onset, "wake_offset_min": wake},
                    "absolute_delta_min": {"onset": onset_delta, "wake": wake_delta},
                    "matches": matches,
                    "evidence_scope": ("physical branch output consistency from stored physical_bed/physical_wake"
                                       if arm == "physical" else "stored feature vector through installed estimator"),
                }
    except (FusionModelError, KeyError, TypeError, ValueError, OverflowError):
        return _unavailable_replay("stored_feature_inference_failed")
    matched = all(item["matches"] for item in comparisons.values()) and len(comparisons) == 6
    return {
        "status": "feature_outputs_match" if matched else "feature_outputs_mismatch",
        "scope": REPLAY_SCOPE,
        "tolerance_min": REPLAY_TOLERANCE_MIN,
        "comparisons": comparisons,
    }


def _model_diagnostics(outcomes, models_dir):
    current = outcomes.get("current_model") if isinstance(outcomes, dict) else None
    if current is None:
        return {"available": False, "reason": "current_model_missing",
                "reporting_scope": "phone_reported_not_independently_replayed",
                "diagnostic_snapshot_scope": "no_current_phone_model_in_this_backup",
                "feature_output_replay": _unavailable_replay("current_model_missing")}
    current = _mapping(current, "current_model")
    predictions = current.get("research_predictions")
    features = current.get("research_feature_snapshot")
    research_personalization = current.get("research_personalization")
    if not isinstance(predictions, dict) or not isinstance(features, dict) or not isinstance(research_personalization, dict):
        raise ValueError("current_model_diagnostics_invalid")
    return {
        "available": True,
        "reporting_scope": "phone_reported_not_independently_replayed",
        "diagnostic_snapshot_scope": "current_phone_model_at_backup_time_not_necessarily_frozen_prospective",
        "model_version": current.get("model_version"),
        "feature_contract": current.get("model_feature_contract"),
        "physical_provider": current.get("physical_provider"),
        "forecast_status": current.get("forecast_status"),
        "cutoff": current.get("cutoff"),
        "input_coverage": _json_copy(current.get("input_coverage", {})),
        "app_personalization": _json_copy(current.get("personalization", {})),
        "model_outputs": _json_copy(predictions),
        "feature_snapshot": _json_copy(features),
        "research_personalization": _json_copy(research_personalization),
        "research_artifacts": _json_copy(current.get("research_artifacts", {})),
        "recommendation_feasibility": _json_copy(current.get("recommendation_feasibility", {})),
        "self_report_scales": _json_copy(current.get("self_report_scales", {})),
        "limitations": _json_copy(current.get("limitations", [])),
        "feature_output_replay": replay_feature_outputs(current, models_dir),
        "observed_outcomes": {
            "recent": _json_copy(current.get("primary_observed_outcomes", {})),
            "baseline": _json_copy(current.get("baseline_observed_outcomes", {})),
            "baseline_comparable": current.get("baseline_comparable"),
            "wearable_auxiliary": _json_copy(current.get("wearable_auxiliary", {})),
        },
        "replay": {
            "status": "unavailable",
            "reason": (
                "PC 배포판에는 Android Kotlin 실행환경과 예측 시점의 완전한 동결 입력이 없어 "
                "사후 재계산을 독립 검증으로 표시하지 않습니다."
            ),
        },
    }


def _prospective_summaries(value):
    if not isinstance(value, list):
        return []
    fields = (
        "night_id", "forecast_status", "cutoff", "generated_at", "model_version",
        "model_feature_contract", "research_predictions", "research_artifacts", "recommendation",
    )
    return [_safe_fields(item, fields) for item in value[-90:] if isinstance(item, dict)]


def _parse_snapshot(snapshot, models_dir):
    snapshot = _mapping(snapshot, "snapshot")
    for field in ("device_id", "participant_key", "day", "revision", "generated_at",
                  "received_at", "sha256", "data"):
        if field not in snapshot:
            raise ValueError(f"snapshot_{field}_missing")
    if not isinstance(snapshot["device_id"], str) or not snapshot["device_id"]:
        raise ValueError("snapshot_device_invalid")
    if not isinstance(snapshot["participant_key"], str) or PARTICIPANT_KEY.fullmatch(snapshot["participant_key"]) is None:
        raise ValueError("snapshot_participant_invalid")
    _day(snapshot["day"])
    _aware(snapshot["generated_at"], "generated_at")
    _aware(snapshot["received_at"], "received_at")
    if type(snapshot["revision"]) is not int or snapshot["revision"] < 1:
        raise ValueError("snapshot_revision_invalid")
    data = _mapping(snapshot["data"], "data")
    if set(data) != {"profile", "records", "recommendation", "forecast", "outcomes"}:
        raise ValueError("snapshot_data_contract_invalid")
    _mapping(data["profile"], "profile")
    if not isinstance(data["records"], list):
        raise ValueError("records_not_list")
    if data["recommendation"] is not None and not isinstance(data["recommendation"], dict):
        raise ValueError("recommendation_invalid")
    if data["forecast"] is not None and not isinstance(data["forecast"], dict):
        raise ValueError("forecast_invalid")
    outcomes = data["outcomes"]
    if outcomes is not None and not isinstance(outcomes, dict):
        raise ValueError("outcomes_invalid")
    outcomes = outcomes or {}
    record_summary, records = _record_summary(data["records"])
    day_view = {
        "day": snapshot["day"],
        "revision": snapshot["revision"],
        "generated_at": snapshot["generated_at"],
        "received_at": snapshot["received_at"],
        "sha256": snapshot["sha256"],
        "validation_scope": snapshot.get("validation_scope", "envelope_and_bounds_only"),
        "analysis_status": snapshot.get("analysis_status", "phone_reported_snapshot_not_independently_verified"),
        "record_summary": record_summary,
        "forecast": _forecast_summary(data["forecast"]),
        "recommendation": _recommendation_summary(data["recommendation"]),
        "outcome_summary": _safe_fields(outcomes, (
            "record_count", "summary_window", "primary_diary", "wearable_auxiliary",
            "realized_comparisons",
        )),
        "model_diagnostics": _model_diagnostics(outcomes, models_dir),
        "prospective_forecasts": _prospective_summaries(outcomes.get("prospective_forecasts")),
    }
    latest_view = dict(day_view)
    latest_view["record_trend"] = _record_trend(records)
    return (snapshot["device_id"], snapshot["participant_key"]), day_view, latest_view


def phone_diagnostics(store, models_dir):
    raw = mobile_snapshots(store, limit=MOBILE_LIMIT)
    aggregate = mobile_summary(store)
    grouped = defaultdict(list)
    rejected = []
    for snapshot in raw:
        try:
            key, day_view, latest_view = _parse_snapshot(snapshot, models_dir)
            grouped[key].append((day_view, latest_view))
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            rejected.append({
                "day": snapshot.get("day") if isinstance(snapshot, dict) else None,
                "received_at": snapshot.get("received_at") if isinstance(snapshot, dict) else None,
                "reason": str(error),
            })
    participants = []
    for (device_id, participant_key), entries in grouped.items():
        entries.sort(key=lambda item: (item[0]["received_at"], item[0]["day"]), reverse=True)
        participants.append({
            "device_id": device_id,
            "participant_key": participant_key,
            "latest_day": entries[0][0]["day"],
            "latest_received_at": entries[0][0]["received_at"],
            "latest": entries[0][1],
            "days": [item[0] for item in entries],
        })
    participants.sort(key=lambda item: item["latest_received_at"], reverse=True)
    return {
        "status": "available" if participants else "no_snapshots",
        "analysis_caveat": (
            "휴대폰이 보고한 계산 결과를 구조 검증해 표시합니다. PC가 같은 예측을 독립 재현했거나 "
            "해당 참여자에서 예측 성능이 입증됐다는 뜻이 아닙니다."
        ),
        "returned_days": len(raw),
        "accepted_days": sum(len(item["days"]) for item in participants),
        "stored_days": aggregate.get("days", 0),
        "participant_count": len(participants),
        "stored_participant_count": aggregate.get("participants", 0),
        "participants": participants,
        "rejected_snapshots": rejected,
    }


def build_console(store, models_dir, now):
    research = research_diagnostics(models_dir)
    phone = phone_diagnostics(store, models_dir)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "scope": {
            "read_only": True,
            "mobile_limit": MOBILE_LIMIT,
            "mobile_truncated": phone["stored_days"] > phone["returned_days"],
            "note": "최근 365개 참여자-일 최신 revision만 조회합니다.",
        },
        "research": research,
        "phone": phone,
    }
