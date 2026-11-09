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
from target_validity import summarize_target_validity
from measurement_audit import audit_measurements
from phone_baseline_evaluation import evaluate_phone_baselines
from full_validity_report import load_full_validity_report
from mobile_backup import audit_history


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
PERSONALIZATION_VALIDATION_FILE = "personalization_validation_v1.json"
CONTINUOUS_CONSTANTS_VALIDATION_FILE = "continuous_constants_validation_v2.json"
TARGET_VALIDITY_VALIDATION_FILE = "target_validity_validation_v1.json"
TARGET_VALIDITY_MODELS = {
    "population", "last", "mean7", "mean14", "weighted28", "weekday",
    "physical", "data", "hybrid", "legacy_data", "legacy_hybrid", "legacy_physical",
}


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


def _load_personalization_validation(path):
    """Load an aggregate-only validation report without inventing absent results."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not_object")
        schema = raw.get("schema_version")
        if schema not in (1, "1"):
            raise ValueError("unsupported_schema")
        if raw.get("artifact_type") not in (None, "sleepwell_personalization_validation"):
            raise ValueError("artifact_type_invalid")
        cohorts = raw.get("cohorts")
        if isinstance(cohorts, dict):
            cohorts = [dict(value, cohort=key) for key, value in cohorts.items()
                       if isinstance(value, dict)]
        if not isinstance(cohorts, list):
            raise ValueError("cohorts_missing")
        safe_cohorts = []
        for cohort in cohorts:
            if not isinstance(cohort, dict) or cohort.get("cohort") not in MODEL_FILES:
                continue
            cohort_name = cohort["cohort"]
            for arm_name in ("data", "hybrid"):
                arm = cohort.get(arm_name)
                if not isinstance(arm, dict):
                    continue
                overall = arm.get("all_test_rows_including_zero_correction")
                if not isinstance(overall, dict):
                    continue
                paired = overall.get("paired_improvement", {})
                paired_metrics = paired.get("metrics", {}) if isinstance(paired, dict) else {}

                def normalize_strata(value):
                    if not isinstance(value, dict):
                        return {}
                    result = {}
                    for label, item in value.items():
                        if not isinstance(item, dict):
                            continue
                        item = _safe_fields(item, (
                            "n_people", "n_nights", "base_metrics", "corrected_metrics",
                            "paired_improvement", "active_nights", "coverage",
                        )) or {}
                        comparison = item.get("paired_improvement")
                        if isinstance(comparison, dict) and isinstance(comparison.get("metrics"), dict):
                            item["paired_improvement"] = _json_copy(comparison["metrics"])
                        result[str(label)] = item
                    return result

                # The report is aggregate research evidence. Participant identifiers and row-level
                # predictions are intentionally not accepted into the console payload.
                safe_cohorts.append({
                    "cohort": cohort_name,
                    "base_arm": arm_name,
                    "base_arm_label": arm.get("base_arm_label"),
                    "n_people": arm.get("n_people"),
                    "n_nights": arm.get("n_nights"),
                    "eligible_personalized_nights": arm.get("eligible_personalized_nights"),
                    "coverage": arm.get("coverage"),
                    "base_metrics": _json_copy(overall.get("base_metrics")),
                    "corrected_metrics": _json_copy(overall.get("corrected_metrics")),
                    "paired_improvement": _json_copy(paired_metrics),
                    "history_strata": normalize_strata(arm.get("history_strata")),
                    "gap_strata": normalize_strata(arm.get("gap_strata")),
                    "qc_strata": normalize_strata(arm.get("qc_strata")),
                    "active_subset_same_rows": _safe_fields(arm.get("active_subset_same_rows"), (
                        "n_people", "n_nights", "base_metrics", "corrected_metrics", "paired_improvement")),
                    "naive_recent_baseline": _json_copy(cohort.get("naive_recent_baseline")),
                    "notes": _json_copy(cohort.get("notes", [])),
                })
        return {
            "available": bool(safe_cohorts),
            "schema_version": schema,
            "generated_at": raw.get("generated_at"),
            "evaluation_status": raw.get("evaluation_status"),
            "target": raw.get("prediction_target"),
            "estimand": (raw.get("evaluation_status") or {}).get("estimand")
                        if isinstance(raw.get("evaluation_status"), dict) else None,
            "policy": _json_copy(raw.get("correction_policy")),
            "cohorts": safe_cohorts,
            "limitations": _json_copy(raw.get("limitations", [])),
            "interpretation": (
                "기존 동결 모델도 과거 수면 이력 특징을 사용합니다. 이 표의 기준은 개인화 없음이 아니라 "
                "추가 온라인 잔차 보정 없음입니다."
            ),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return {
            "available": False,
            "reason": type(error).__name__,
            "interpretation": "검증 산출물이 없거나 계약을 확인할 수 있어 결과를 표시하지 않습니다.",
        }


def _load_continuous_constants_validation(path):
    """Load the aggregate comparison of the frozen 14-day and daily constants."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(raw, dict) or raw.get("schema_version") not in (1, "1")
                or raw.get("artifact_type") != "sleepwell_continuous_constants_validation"):
            raise ValueError("invalid_continuous_constants_artifact")
        cohorts = _mapping(raw.get("cohorts"), "cohorts")
        safe_cohorts = []
        for cohort_name in MODEL_FILES:
            cohort = _mapping(cohorts.get(cohort_name), f"{cohort_name}_cohort")
            scores = _mapping(cohort.get("scores"), f"{cohort_name}_scores")
            safe_scores = {}
            for policy in ("legacy", "daily"):
                values = _mapping(scores.get(policy), f"{cohort_name}_{policy}_scores")
                safe_scores[policy] = {
                    arm: _number(values.get(arm), f"{cohort_name}_{policy}_{arm}")
                    for arm in ("data", "hybrid", "physical")
                }
            comparisons = _mapping(cohort.get("comparisons"), f"{cohort_name}_comparisons")
            safe_comparisons = {}
            for arm in ("data", "hybrid"):
                comparison = _mapping(comparisons.get(arm), f"{cohort_name}_{arm}_comparison")
                ci = comparison.get("ci95_min")
                if not isinstance(ci, list) or len(ci) != 2:
                    raise ValueError(f"{cohort_name}_{arm}_ci_invalid")
                safe_comparisons[arm] = {
                    "gain_min": _number(comparison.get("gain_min"), f"{cohort_name}_{arm}_gain"),
                    "ci95_min": [_number(value, f"{cohort_name}_{arm}_ci") for value in ci],
                    "holm_p": _number(comparison.get("holm_p"), f"{cohort_name}_{arm}_holm_p"),
                }
            physics = _mapping(cohort.get("physics_addition"), f"{cohort_name}_physics_addition")
            physics_ci = physics.get("ci95_min")
            if not isinstance(physics_ci, list) or len(physics_ci) != 2:
                raise ValueError(f"{cohort_name}_physics_ci_invalid")
            safe_cohorts.append({
                "cohort": cohort_name,
                "selected_policy": cohort.get("selected_policy"),
                "n_people": _number(cohort.get("n_people"), f"{cohort_name}_n_people"),
                "n_nights": _number(cohort.get("n_nights"), f"{cohort_name}_n_nights"),
                "legacy_active_nights": _number(
                    cohort.get("legacy_active_nights"), f"{cohort_name}_legacy_active_nights"),
                "daily_active_nights": _number(
                    cohort.get("daily_active_nights"), f"{cohort_name}_daily_active_nights"),
                "max_history": _number(cohort.get("max_history"), f"{cohort_name}_max_history"),
                "scores": safe_scores,
                "comparisons": safe_comparisons,
                "validation_gate": cohort.get("validation_gate") is True,
                "validation_gain_min": _number(
                    cohort.get("validation_gain_min"), f"{cohort_name}_validation_gain_min"),
                "physics_addition": {
                    "gain_min": _number(physics.get("gain_min"), f"{cohort_name}_physics_gain"),
                    "ci95_min": [_number(value, f"{cohort_name}_physics_ci") for value in physics_ci],
                },
            })
        return {
            "available": True,
            "schema_version": raw.get("schema_version"),
            "version": raw.get("version"),
            "test_status": raw.get("test_status"),
            "interpretation": raw.get("interpretation"),
            "cohorts": safe_cohorts,
            "no_service_promotion": raw.get("no_service_promotion") is True,
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return {
            "available": False,
            "reason": type(error).__name__,
            "interpretation": "매일 갱신 상수의 집계 연구 산출물이 없거나 계약을 확인할 수 없습니다.",
        }


def _exact_keys(value, expected, field):
    value = _mapping(value, field)
    if set(value) != set(expected):
        raise ValueError(f"{field}_contract_invalid")
    return value


def _count(value, field):
    if type(value) is not int or value < 0:
        raise ValueError(f"{field}_count_invalid")
    return value


def _nonnegative_number(value, field):
    value = _number(value, field)
    if value < 0:
        raise ValueError(f"{field}_negative")
    return value


def _bounded_text(value, field, maximum=1000):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field}_text_invalid")
    return value


def _load_target_validity_validation(path):
    """Load only the aggregate target-validity contract; row-level data is rejected."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        _exact_keys(raw, {
            "schema_version", "artifact_type", "version", "test_status",
            "no_service_promotion", "cohorts", "limitations", "provenance",
        }, "target_validity")
        if (type(raw["schema_version"]) is not int or raw["schema_version"] != 1
                or raw["artifact_type"] != "sleepwell_target_validity_validation"
                or raw["version"] != "2026-10-25-v1"
                or raw["test_status"] != "retrospective_reused_test"
                or raw["no_service_promotion"] is not True):
            raise ValueError("target_validity_identity_invalid")
        cohorts = _exact_keys(raw["cohorts"], MODEL_FILES, "target_validity_cohorts")
        safe_cohorts = []
        for cohort_name in MODEL_FILES:
            cohort = _exact_keys(cohorts[cohort_name], {
                "n_people", "n_nights", "label_source", "models", "history_strata",
                "feasibility", "availability_assumption",
            }, f"target_validity_{cohort_name}")
            if not isinstance(cohort["models"], list) or not cohort["models"]:
                raise ValueError(f"target_validity_{cohort_name}_models_invalid")
            models = []
            seen_models = set()
            for index, item in enumerate(cohort["models"]):
                item = _exact_keys(item, {
                    "name", "joint_mae_min", "onset_mae_min", "wake_mae_min",
                    "skill_vs_recent14", "gain_vs_recent14_min", "ci95_min",
                    "participant_improved_fraction",
                }, f"target_validity_{cohort_name}_model_{index}")
                name = item["name"]
                if name not in TARGET_VALIDITY_MODELS or name in seen_models:
                    raise ValueError(f"target_validity_{cohort_name}_model_name_invalid")
                seen_models.add(name)
                ci = item["ci95_min"]
                if not isinstance(ci, list) or len(ci) != 2:
                    raise ValueError(f"target_validity_{cohort_name}_ci_invalid")
                safe_ci = [_number(value, f"target_validity_{cohort_name}_ci") for value in ci]
                if safe_ci[0] > safe_ci[1]:
                    raise ValueError(f"target_validity_{cohort_name}_ci_order_invalid")
                improved = _number(
                    item["participant_improved_fraction"],
                    f"target_validity_{cohort_name}_participant_improved_fraction")
                if not 0 <= improved <= 1:
                    raise ValueError(f"target_validity_{cohort_name}_fraction_invalid")
                models.append({
                    "name": name,
                    "joint_mae_min": _nonnegative_number(item["joint_mae_min"], f"{cohort_name}_{name}_joint"),
                    "onset_mae_min": _nonnegative_number(item["onset_mae_min"], f"{cohort_name}_{name}_onset"),
                    "wake_mae_min": _nonnegative_number(item["wake_mae_min"], f"{cohort_name}_{name}_wake"),
                    "skill_vs_recent14": _number(item["skill_vs_recent14"], f"{cohort_name}_{name}_skill"),
                    "gain_vs_recent14_min": _number(item["gain_vs_recent14_min"], f"{cohort_name}_{name}_gain"),
                    "ci95_min": safe_ci,
                    "participant_improved_fraction": improved,
                })
            if not isinstance(cohort["history_strata"], list):
                raise ValueError(f"target_validity_{cohort_name}_history_invalid")
            history = []
            for index, item in enumerate(cohort["history_strata"]):
                item = _exact_keys(item, {
                    "label", "n_people", "n_nights", "data_mae_min", "hybrid_mae_min",
                    "baseline_mae_min",
                }, f"target_validity_{cohort_name}_history_{index}")
                history.append({
                    "label": _bounded_text(item["label"], f"{cohort_name}_history_label", 100),
                    "n_people": _count(item["n_people"], f"{cohort_name}_history_people"),
                    "n_nights": _count(item["n_nights"], f"{cohort_name}_history_nights"),
                    "data_mae_min": _nonnegative_number(item["data_mae_min"], f"{cohort_name}_history_data"),
                    "hybrid_mae_min": _nonnegative_number(item["hybrid_mae_min"], f"{cohort_name}_history_hybrid"),
                    "baseline_mae_min": _nonnegative_number(item["baseline_mae_min"], f"{cohort_name}_history_baseline"),
                })
            feasibility = _exact_keys(cohort["feasibility"], {
                "max_prior_records", "people_with_14_prior_records", "people_with_28_prior_records",
            }, f"target_validity_{cohort_name}_feasibility")
            safe_cohorts.append({
                "cohort": cohort_name,
                "n_people": _count(cohort["n_people"], f"{cohort_name}_n_people"),
                "n_nights": _count(cohort["n_nights"], f"{cohort_name}_n_nights"),
                "label_source": _bounded_text(cohort["label_source"], f"{cohort_name}_label_source"),
                "models": models,
                "history_strata": history,
                "feasibility": {
                    key: _count(value, f"{cohort_name}_{key}") for key, value in feasibility.items()
                },
                "availability_assumption": _bounded_text(
                    cohort["availability_assumption"], f"{cohort_name}_availability_assumption"),
            })
        limitations = raw["limitations"]
        if not isinstance(limitations, list):
            raise ValueError("target_validity_limitations_invalid")
        safe_limitations = [
            _bounded_text(value, "target_validity_limitation") for value in limitations
        ]
        _mapping(raw["provenance"], "target_validity_provenance")
        return {
            "available": True,
            "schema_version": 1,
            "version": raw["version"],
            "test_status": raw["test_status"],
            "no_service_promotion": True,
            "cohorts": safe_cohorts,
            "limitations": safe_limitations,
            "interpretation": (
                "최근 기록을 매일 반영하는 상수 정책에서 선택된 연구 모델과 최근 14일 기준선의 "
                "후향 비교입니다. 현재 서비스의 robust-v2 모델을 대체하거나 승격하지 않습니다."
            ),
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        return {
            "available": False,
            "reason": type(error).__name__,
            "interpretation": "기준선 비교 집계 산출물이 없거나 엄격한 계약 검증에 실패했습니다.",
        }


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
        "personalization_validation": _load_personalization_validation(
            models_dir / PERSONALIZATION_VALIDATION_FILE),
        "continuous_constants_validation": _load_continuous_constants_validation(
            models_dir / CONTINUOUS_CONSTANTS_VALIDATION_FILE),
        "target_validity_validation": _load_target_validity_validation(
            models_dir / TARGET_VALIDITY_VALIDATION_FILE),
        "full_validity_report": load_full_validity_report(models_dir / "full_validity_tables_v1.json"),
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
            "parameter_semantics": {
                "physiological_constants": "fixed_not_identified",
                "phase": "behavioral_midpoint_not_DLMO",
                "target_sleep": "heuristic_not_estimated_optimal_need",
            },
            "research_questions": {
                "prediction": "실제 다음 주수면의 입면·최종 기상 시각",
                "recommendation": "일정 제약 안에서 확보할 수면 기회와 취침·기상 계획",
                "effect": "권장 노출 후 독립 관측한 규칙성·주관 기능 변화; 별도 대조 검증 필요",
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
    days = sorted({date.fromisoformat(record["day"]) for record in valid})
    latest = days[-1].isoformat() if days else None
    span = (days[-1] - days[0]).days + 1 if days else 0
    gaps = [(right - left).days - 1 for left, right in zip(days, days[1:])]
    return {
        "count": len(valid),
        "observed_days": len(days),
        "source_counts": dict(sorted(counts.items())),
        "source_roles": dict(sorted(roles.items())),
        "latest_date": latest,
        "calendar_span_days": span,
        "observed_day_coverage": round(len(days) / span, 3) if span else None,
        "maximum_unobserved_gap_days": max(gaps, default=0) if len(days) > 1 else None,
        "quality_note": (
            "날짜 범위 내 기록 존재율이며 착용률·일지 진실성 또는 정답 품질을 뜻하지 않습니다."
        ),
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
        "subjective_quality", "process_s", "plan_feasible", "explanation", "viewed_at",
        "exposed_at", "exposure_status", "policy_version", "target_duration_minutes",
    ))


def _finite_pair(value, onset_key, wake_key):
    if not isinstance(value, dict):
        return None
    onset, wake = value.get(onset_key), value.get(wake_key)
    if (type(onset) not in (int, float) or not math.isfinite(onset)
            or type(wake) not in (int, float) or not math.isfinite(wake)):
        return None
    return {"onset_offset_min": float(onset), "wake_offset_min": float(wake)}


def _sidecar_evidence(forecast):
    """Normalize only the two registered residual-sidecar formats without relabeling them."""
    android = forecast.get("adaptive_research_predictions")
    if isinstance(android, dict):
        arms = []
        for arm, value in android.items():
            if not isinstance(value, dict):
                continue
            base = _finite_pair(value, "base_onset_offset_min", "base_wake_offset_min")
            corrected = _finite_pair(value, "adjusted_onset_offset_min", "adjusted_wake_offset_min")
            if base is None or corrected is None:
                continue
            adjustment = value.get("adjustment") if isinstance(value.get("adjustment"), dict) else {}
            arms.append({"arm": arm, "base": base, "corrected": corrected,
                         "active": adjustment.get("active"),
                         "eligible_count": adjustment.get("eligible_count")})
        first = next((value.get("adjustment") for value in android.values()
                      if isinstance(value, dict) and isinstance(value.get("adjustment"), dict)), {})
        return {"source": "android_adaptive_research_predictions",
                "status": "reported_sidecar" if arms else "invalid_or_empty",
                "policy_version": first.get("policy_version"), "base_artifact_identity_sha256": None,
                "snapshot_sha256": None, "arms": arms}

    windows = forecast.get("online_personalization")
    if isinstance(windows, dict):
        base_predictions = forecast.get("predictions")
        corrected_predictions = windows.get("predictions")
        arms = []
        if isinstance(base_predictions, dict) and isinstance(corrected_predictions, dict):
            for arm, corrected_value in corrected_predictions.items():
                base = _finite_pair(base_predictions.get(arm), "onset_offset_min", "wake_offset_min")
                corrected = _finite_pair(corrected_value, "onset_offset_min", "wake_offset_min")
                if base is None or corrected is None:
                    continue
                adjustment = windows.get("adjustments", {}).get(arm, {})
                arms.append({"arm": arm, "base": base, "corrected": corrected,
                             "active": adjustment.get("active"),
                             "eligible_count": adjustment.get("eligible_count")})
        return {"source": "windows_online_personalization",
                "status": windows.get("status") or ("available" if windows.get("available") else "unavailable"),
                "policy_version": windows.get("policy_version"),
                "base_artifact_identity_sha256": windows.get("base_artifact_identity_sha256"),
                "snapshot_sha256": windows.get("snapshot_sha256"), "arms": arms}
    return None


def _prospective_outcome_summaries(value):
    if not isinstance(value, list):
        return []
    fields = ("night_id", "record_id", "status", "exclusion_reason", "actual_source", "target",
              "available_at", "outcome_at", "actual_onset_offset_min", "actual_wake_offset_min",
              "source_model_version", "source_model_feature_contract", "source_research_artifacts",
              "rule", "residuals")
    return [_safe_fields(item, fields) for item in value[-90:] if isinstance(item, dict)]


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
        "parameter_semantics": _json_copy(current.get("parameter_semantics", {})),
        "uncertainty": _json_copy(current.get("uncertainty", {})),
        "model_outputs": _json_copy(predictions),
        "feature_snapshot": _json_copy(features),
        "research_personalization": _json_copy(research_personalization),
        "research_artifacts": _json_copy(current.get("research_artifacts", {})),
        "recommendation_feasibility": _json_copy(current.get("recommendation_feasibility", {})),
        "self_report_scales": _json_copy(current.get("self_report_scales", {})),
        "recommendation_exposure": _json_copy(current.get("recommendation_exposure", {})),
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
        "recommendation_exposure", "actual_outcome", "realized_comparison", "predictions",
        "fusion_model", "adaptive_research_predictions", "online_personalization", "research_baselines",
    )
    summaries = []
    for item in value[-90:]:
        if not isinstance(item, dict):
            continue
        summary = _safe_fields(item, fields)
        evidence = _sidecar_evidence(item)
        if evidence is not None:
            summary["sidecar_evidence"] = evidence
        summaries.append(summary)
    return summaries


def _parse_snapshot(snapshot, models_dir, history_snapshots=None):
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
    target_validity = summarize_target_validity(_json_copy(records), snapshot["generated_at"])
    measurement_audit = audit_measurements(_json_copy(data["records"]), snapshot["received_at"],
        profile=_json_copy(data["profile"]), snapshots=history_snapshots)
    measurement_audit["snapshot_history_scope"] = "at_most_500_oldest_retained_immutable_backup_versions_not_initial_registration_log"
    baseline_evaluation, reporting_lag = evaluate_phone_baselines(outcomes, snapshot["generated_at"], models_dir=models_dir)
    measurement_audit["first_outcome_reporting_lag"] = reporting_lag
    day_view = {
        "day": snapshot["day"],
        "revision": snapshot["revision"],
        "generated_at": snapshot["generated_at"],
        "received_at": snapshot["received_at"],
        "sha256": snapshot["sha256"],
        "validation_scope": snapshot.get("validation_scope", "envelope_and_bounds_only"),
        "analysis_status": snapshot.get("analysis_status", "phone_reported_snapshot_not_independently_verified"),
        "record_summary": record_summary,
        "target_validity": target_validity,
        "measurement_audit": measurement_audit,
        "research_baseline_evaluation": baseline_evaluation,
        "forecast": _forecast_summary(data["forecast"]),
        "recommendation": _recommendation_summary(data["recommendation"]),
        "outcome_summary": _safe_fields(outcomes, (
            "record_count", "summary_window", "primary_diary", "wearable_auxiliary",
            "realized_comparisons", "recommendation_exposures", "habit_outcomes",
        )),
        "model_diagnostics": _model_diagnostics(outcomes, models_dir),
        "prospective_forecasts": _prospective_summaries(outcomes.get("prospective_forecasts")),
        "prospective_outcomes": _prospective_outcome_summaries(outcomes.get("prospective_outcomes")),
    }
    latest_view = dict(day_view)
    latest_view["record_trend"] = _record_trend(records)
    return (snapshot["device_id"], snapshot["participant_key"]), day_view, latest_view


def phone_diagnostics(store, models_dir):
    raw = mobile_snapshots(store, limit=MOBILE_LIMIT)
    aggregate = mobile_summary(store)
    grouped = defaultdict(list)
    audit_cache = {}
    rejected = []
    for snapshot in raw:
        try:
            identity=(snapshot.get("device_id"),snapshot.get("participant_key"))
            if identity not in audit_cache:
                audit_cache[identity]=audit_history(store,*identity)
            histories=audit_cache[identity]['snapshots']
            key, day_view, latest_view = _parse_snapshot(snapshot, models_dir, histories)
            day_view["measurement_audit"]["snapshot_history_truncated"]=audit_cache[identity]['truncated']
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
        "evidence_contract": {
            "prediction": "20시 이전 입력으로 고정 저장되고 실제 결과와 연결된 예측만 전향 평가",
            "recommendation": "생성 시각과 실제 노출 시각을 구분; 노출되지 않은 권장은 효과 평가에서 제외",
            "effect": "모델 S가 아니라 직접 일지의 규칙성·개운함·피로도 등 독립 결과를 사용",
            "causal_status": "기술통계; 무작위 대조 또는 사전 지정 비교 전에는 앱 효과로 해석하지 않음",
        },
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
