"""Descriptive, post-enrollment sleep-habit outcome summaries.

The primary outcomes use direct diary/self-report records. Device-imported sleep
is reported separately as an auxiliary signal because wear time and device
processing can change the observed timing. The module intentionally performs no
significance test and makes no causal or clinical-improvement claim.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta
from statistics import fmean
from zoneinfo import ZoneInfo


ZONE = ZoneInfo("Asia/Seoul")
PRIMARY_SOURCE = "diary"
AUXILIARY_SOURCES = ("health_connect", "healthkit")
DEFAULT_SUBJECTIVE_CONTRACT = "legacy_diary_rested_1_5_fatigue_0_10"


def _datetime(value) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=ZONE) if parsed.tzinfo is None else parsed.astimezone(ZONE)


def _clock_minutes(value) -> float:
    local = _datetime(value)
    return local.hour * 60 + local.minute + local.second / 60


def _circular_sd_minutes(values: list[float]) -> float | None:
    # One observation does not establish regularity.  Reporting zero here would
    # present absence of variation evidence as a perfectly regular schedule.
    if len(values) < 2:
        return None
    angles = [2 * math.pi * value / 1440 for value in values]
    resultant = math.hypot(fmean(math.cos(x) for x in angles), fmean(math.sin(x) for x in angles))
    resultant = min(1.0, max(resultant, 1e-12))
    return round(math.sqrt(-2 * math.log(resultant)) * 1440 / (2 * math.pi), 1)


def _latest_post_enrollment(profile: dict, records: list[dict], now: datetime) -> list[dict]:
    enrolled = _datetime(profile["created_at"])
    latest: dict[tuple[str, str], tuple[datetime, str, dict]] = {}
    for record in records:
        source = record.get("source")
        if source not in (PRIMARY_SOURCE, *AUXILIARY_SOURCES):
            continue
        try:
            bed = _datetime(record["bed"])
            wake = _datetime(record["wake"])
            observed = _datetime(record.get("observed_at") or record["wake"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        duration_min = (wake - bed).total_seconds() / 60
        if (wake < enrolled or wake > now or observed > now
                or not 30 <= duration_min <= 960):
            continue
        wake_date = wake.date().isoformat()
        canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        candidate = (observed, canonical, record)
        # Diary and wearable observations have different evidentiary roles and
        # must never displace each other merely because they share a wake date.
        key = (source, wake_date)
        if key not in latest or candidate[:2] > latest[key][:2]:
            latest[key] = candidate
    return [latest[key][2] for key in sorted(latest)]


def _in_window(records: list[dict], start: date, end: date, sources: tuple[str, ...]) -> list[dict]:
    # A source group contributes at most one observation per wake date.  This
    # keeps 14-day coverage bounded at 14 while preserving diary vs wearable
    # separation (the caller requests those groups independently).
    latest = {}
    for record in records:
        if record.get("source") not in sources:
            continue
        try:
            wake_day = _datetime(record["wake"]).date()
            observed = _datetime(record.get("observed_at") or record["wake"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not start <= wake_day <= end:
            continue
        canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        candidate = (observed, canonical, record)
        if wake_day not in latest or candidate[:2] > latest[wake_day][:2]:
            latest[wake_day] = candidate
    return [latest[key][2] for key in sorted(latest)]


def _subjective_values(records: list[dict], field: str, low: float, high: float) -> list[float]:
    values = []
    for record in records:
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = float(value)
        if math.isfinite(value) and low <= value <= high:
            values.append(value)
    return values


def _subjective_contract(record: dict) -> str:
    value = record.get("subjective_scale") or record.get("subjective_scale_contract")
    return value if isinstance(value, str) and value else DEFAULT_SUBJECTIVE_CONTRACT


def _window_summary(records: list[dict], start: date, end: date, subjective: bool) -> dict:
    bed_values = [_clock_minutes(record["bed"]) for record in records]
    wake_values = [_clock_minutes(record["wake"]) for record in records]
    summary = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "expected_days": 14,
        "recorded_days": len(records),
        "coverage_rate": round(len(records) / 14, 3),
        "missing_sleep_days": 14 - len(records),
        "bedtime_circular_sd_min": _circular_sd_minutes(bed_values),
        "wake_circular_sd_min": _circular_sd_minutes(wake_values),
    }
    if subjective:
        rested = _subjective_values(records, "rested", 1, 5)
        contracts = sorted({_subjective_contract(record) for record in records})
        fatigue_by_contract = {
            contract: _subjective_values(
                [record for record in records if _subjective_contract(record) == contract],
                "fatigue", 1 if "fatigue_1_5" in contract else 0,
                5 if "fatigue_1_5" in contract else 10,
            )
            for contract in contracts
        }
        fatigue = next(iter(fatigue_by_contract.values()), []) if len(fatigue_by_contract) <= 1 else []
        fatigue_response_days = sum(len(values) for values in fatigue_by_contract.values())
        summary.update({
            "rested_mean": round(fmean(rested), 2) if rested else None,
            "fatigue_mean": round(fmean(fatigue), 2) if fatigue else None,
            "rested_response_days": len(rested),
            "fatigue_response_days": fatigue_response_days,
            "missing_rested_days": 14 - len(rested),
            "missing_fatigue_days": 14 - fatigue_response_days,
            "subjective_scale_contracts": contracts,
            "fatigue_scales_comparable": len(contracts) <= 1,
            "fatigue_by_scale": {
                contract: {
                    "response_days": len(values),
                    "mean": round(fmean(values), 2) if values else None,
                }
                for contract, values in fatigue_by_contract.items()
            },
        })
    return summary


def summarize_recommendation_effects(profile: dict | None, records: list[dict], recommendations: list[dict], exposures: list[dict], now: datetime) -> dict:
    """Summarize follow-up only after a recommendation was actually viewed before bed."""
    empty = {"available": False, "causal_effect_estimate": None}
    if not profile or not profile.get("created_at"):
        return dict(empty, status="no_profile", reason="연구 시작 정보가 없습니다.")
    if not profile.get("enrollment_id"):
        return dict(empty, status="enrollment_identity_missing", reason="현재 가입 기간을 식별할 수 없어 추천 후속 결과를 연결하지 않았습니다.")

    now = _datetime(now)
    try:
        enrolled = _datetime(profile["created_at"])
    except (TypeError, ValueError, OverflowError):
        return dict(empty, status="invalid_enrollment_time", reason="연구 시작 시각을 확인할 수 없습니다.")
    if enrolled > now:
        return dict(empty, status="enrollment_not_started", reason="연구 시작 시각이 아직 도래하지 않았습니다.")
    enrollment_id = profile["enrollment_id"]

    excluded = {
        "unsupported_source": 0,
        "different_enrollment": 0,
        "unknown_availability_time": 0,
        "future_or_not_yet_available": 0,
        "invalid_record": 0,
        "superseded_versions": 0,
    }
    latest = {}
    for record in records or []:
        if record.get("source") != PRIMARY_SOURCE:
            excluded["unsupported_source"] += 1
            continue
        if record.get("enrollment_id") != enrollment_id:
            excluded["different_enrollment"] += 1
            continue
        raw_available = record.get("observed_at") or record.get("received_at")
        if not raw_available:
            excluded["unknown_availability_time"] += 1
            continue
        try:
            bed = _datetime(record["bed"])
            wake = _datetime(record["wake"])
            available = _datetime(raw_available)
        except (KeyError, TypeError, ValueError, OverflowError):
            excluded["invalid_record"] += 1
            continue
        duration_min = (wake - bed).total_seconds() / 60
        if wake < enrolled or wake > now or available > now:
            excluded["future_or_not_yet_available"] += 1
            continue
        if available < wake or not 30 <= duration_min <= 960:
            excluded["invalid_record"] += 1
            continue
        canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        candidate = (available, canonical, record)
        key = wake.date().isoformat()
        if key in latest:
            excluded["superseded_versions"] += 1
        if key not in latest or candidate[:2] > latest[key][:2]:
            latest[key] = candidate
    eligible = [latest[key][2] for key in sorted(latest)]

    exposure_rows = {}
    exposure_iterable = ({"recommendation_id": key, "viewed_at": value} for key, value in exposures.items()) if isinstance(exposures, dict) else (exposures or [])
    for exposure in exposure_iterable:
        if not isinstance(exposure, dict) or exposure.get("recommendation_id") is None:
            continue
        exposure_rows.setdefault(str(exposure["recommendation_id"]), []).append(exposure.get("viewed_at"))

    def recommendation_enrollment(recommendation):
        snapshot = recommendation.get("profile_snapshot")
        if isinstance(snapshot, dict) and snapshot.get("enrollment_id"):
            return snapshot["enrollment_id"]
        return recommendation.get("enrollment_id")

    counts = {
        "total_eligible_records": len(eligible),
        "viewed_before_sleep": 0,
        "viewed_late_or_at_sleep": 0,
        "generated_but_unseen": 0,
        "exposure_timing_unknown": 0,
        "no_matching_recommendation": 0,
    }
    exposed = []
    for record in eligible:
        actual_bed = _datetime(record["bed"])
        wake_day = _datetime(record["wake"]).date()
        candidates = []
        for recommendation in recommendations or []:
            if not isinstance(recommendation, dict) or recommendation_enrollment(recommendation) != enrollment_id:
                continue
            try:
                generated = _datetime(recommendation["generated_at"])
                recommended_bed = _datetime(recommendation["bed"])
                recommended_wake = _datetime(recommendation["wake"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if generated > now or generated >= actual_bed or recommended_wake.date() != wake_day:
                continue
            candidates.append((recommendation, generated, recommended_bed))
        if not candidates:
            counts["no_matching_recommendation"] += 1
            continue

        before_sleep = []
        late = False
        timing_unknown = False
        for recommendation, generated, recommended_bed in candidates:
            identity = recommendation.get("id")
            raw_views = exposure_rows.get(str(identity), []) if identity is not None else []
            for raw_view in raw_views:
                try:
                    viewed = _datetime(raw_view)
                except (TypeError, ValueError, OverflowError):
                    timing_unknown = True
                    continue
                if viewed < generated or viewed > now:
                    timing_unknown = True
                elif viewed < actual_bed:
                    before_sleep.append((viewed, generated, recommendation, recommended_bed))
                else:
                    late = True
        if before_sleep:
            _, _, recommendation, recommended_bed = max(before_sleep, key=lambda item: (item[0], item[1], json.dumps(item[2], ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))))
            exposed.append((record, recommendation, recommended_bed))
            counts["viewed_before_sleep"] += 1
        elif late:
            counts["viewed_late_or_at_sleep"] += 1
        elif timing_unknown:
            counts["exposure_timing_unknown"] += 1
        else:
            counts["generated_but_unseen"] += 1

    def scale_contract(record, field):
        defaults = {
            "rested": (DEFAULT_SUBJECTIVE_CONTRACT, 1.0, 5.0),
            "fatigue": (DEFAULT_SUBJECTIVE_CONTRACT, 0.0, 10.0),
            "sleepiness": ("sleepwell_custom_not_validated_sleepiness_0_10", 0.0, 10.0),
            "burden": ("sleepwell_custom_not_validated_burden_0_10", 0.0, 10.0),
        }
        scales = record.get("subjective_scales")
        specification = scales.get(field) if isinstance(scales, dict) else None
        if isinstance(specification, dict):
            low, high = specification.get("min"), specification.get("max")
            if (not isinstance(low, bool) and not isinstance(high, bool) and isinstance(low, (int, float)) and isinstance(high, (int, float)) and math.isfinite(float(low)) and math.isfinite(float(high)) and low < high):
                instrument = scales.get("instrument") or "unspecified_instrument"
                return f"{instrument}:{field}_{float(low):g}_{float(high):g}", float(low), float(high)
        declared = record.get("subjective_scale") or record.get("subjective_scale_contract")
        if isinstance(declared, str) and declared:
            lowered = declared.lower()
            low, high = defaults[field][1:]
            if f"{field}_1_5" in lowered or (field == "fatigue" and "fatigue_1_5" in lowered):
                low, high = 1.0, 5.0
            elif f"{field}_0_10" in lowered:
                low, high = 0.0, 10.0
            return declared, low, high
        return defaults[field]

    def valid_subjective(record, field):
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        contract, low, high = scale_contract(record, field)
        return (contract, value) if math.isfinite(value) and low <= value <= high else None

    def subjective_summary(field):
        by_scale = {}
        for record, _, _ in exposed:
            response = valid_subjective(record, field)
            if response is not None:
                by_scale.setdefault(response[0], []).append(response[1])
        response_count = sum(len(values) for values in by_scale.values())
        comparable = len(by_scale) <= 1
        values = next(iter(by_scale.values()), []) if comparable else []
        return {
            "response_count": response_count,
            "missing_count": len(exposed) - response_count,
            "mean": round(fmean(values), 2) if values else None,
            "scales_comparable": comparable,
            "scale_contracts": sorted(by_scale),
            "by_scale": {contract: {"response_count": len(values), "mean": round(fmean(values), 2)} for contract, values in sorted(by_scale.items())},
        }

    outcomes = {field: subjective_summary(field) for field in ("rested", "fatigue", "sleepiness", "burden")}
    deviations = [abs((_datetime(record["bed"]) - recommended_bed).total_seconds() / 60) for record, _, recommended_bed in exposed]
    response_records = [record for record, _, _ in exposed if any(valid_subjective(record, field) is not None for field in ("rested", "fatigue", "sleepiness", "burden"))]
    known_survey_timing = 0
    for record in response_records:
        if not record.get("survey_occurred_at"):
            continue
        try:
            survey_time = _datetime(record["survey_occurred_at"])
            wake = _datetime(record["wake"])
            available = _datetime(record.get("observed_at") or record["received_at"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if wake <= survey_time <= available <= now:
            known_survey_timing += 1
    policy_versions = {}
    for _, recommendation, _ in exposed:
        version = recommendation.get("policy_version") or "unversioned"
        policy_versions[version] = policy_versions.get(version, 0) + 1

    return {
        "available": True,
        "status": "observational_only",
        "causal_status": "observational_only",
        "causal_effect_estimate": None,
        "randomized_assignment": False,
        "enrollment_id": enrollment_id,
        "as_of": now.isoformat(),
        "eligible_diary_records": len(eligible),
        "excluded_records": excluded,
        "exposure_counts": counts,
        "descriptive_follow_up": {
            "status": "descriptive_only" if exposed else "no_exposed_follow_up",
            "exposed_records": len(exposed),
            "absolute_bedtime_deviation_min": {"response_count": len(deviations), "mean": round(fmean(deviations), 2) if deviations else None},
            "subjective_outcomes": outcomes,
            "survey_timing": {
                "response_record_count": len(response_records),
                "known_count": known_survey_timing,
                "unknown_count": len(response_records) - known_survey_timing,
                "basis": "participant_entered_survey_occurred_at_only_no_imputation",
            },
            "policy_versions": policy_versions,
        },
        "forecast_accuracy": {
            "status": "separate_prospective_evaluation_required",
            "evaluated_forecasts": 0,
            "individual_interval_validated": False,
            "mae_used_as_individual_interval": False,
            "reason": "권장안 노출 후 기술통계는 사전 고정 예측의 정확도나 개인 예측구간 검증이 아닙니다.",
        },
        "effect_study_prerequisites": {
            "consent": "required_not_recorded",
            "predeclared_assignment": "required_not_implemented",
            "future_follow_up": "required_not_started",
            "operational_trial": False,
        },
        "limitations": [
            "추천을 실제로 본 시각이 취침 전인 기록만 노출 후 기술통계에 포함합니다.",
            "생성됐지만 보지 않은 추천과 취침 후 본 추천은 준수로 간주하지 않습니다.",
            "설문 발생 시각이 없으면 기상·수신 시각으로 대체하지 않습니다.",
            "서로 다른 주관 척도는 하나의 평균으로 합치지 않습니다.",
            "무작위 배정이 없어 집단 인과 효과나 p값을 계산하지 않습니다.",
        ],
    }


def summarize_habits(profile: dict | None, records: list[dict], now: datetime) -> dict:
    """Return deterministic first-14-day and recent-14-day descriptive outcomes."""
    if not profile or not profile.get("created_at"):
        return {"available": False, "status": "no_profile", "reason": "연구 시작 정보가 없습니다."}

    now = _datetime(now)
    enrolled = _datetime(profile["created_at"])
    first_start = enrolled.date()
    first_end = first_start + timedelta(days=13)
    recent_end = now.date()
    recent_start = recent_end - timedelta(days=13)
    elapsed_days = max(0, (now.date() - first_start).days + 1)
    comparison_available = elapsed_days >= 28 and recent_start > first_end
    eligible = _latest_post_enrollment(profile, records, now)

    primary_first = _window_summary(
        _in_window(eligible, first_start, first_end, (PRIMARY_SOURCE,)), first_start, first_end, True)
    primary_recent = _window_summary(
        _in_window(eligible, recent_start, recent_end, (PRIMARY_SOURCE,)), recent_start, recent_end, True)

    auxiliary_first_records = _in_window(eligible, first_start, first_end, AUXILIARY_SOURCES)
    auxiliary_recent_records = _in_window(eligible, recent_start, recent_end, AUXILIARY_SOURCES)
    auxiliary = {
        "role": "착용·기기 처리의 영향을 받는 보조 수면시각 지표",
        "first_14_days": _window_summary(auxiliary_first_records, first_start, first_end, False),
        "recent_14_days": _window_summary(auxiliary_recent_records, recent_start, recent_end, False),
        "by_source": {},
    }
    for source in AUXILIARY_SOURCES:
        auxiliary["by_source"][source] = {
            "first_14_days": _window_summary(
                _in_window(eligible, first_start, first_end, (source,)), first_start, first_end, False),
            "recent_14_days": _window_summary(
                _in_window(eligible, recent_start, recent_end, (source,)), recent_start, recent_end, False),
        }

    reason = ("첫 14일과 겹치지 않는 최근 14일이 아직 완성되지 않았습니다."
              if not comparison_available else
              "두 기간의 기술통계입니다. 통계적 유의성이나 SleepWell의 인과 효과를 뜻하지 않습니다.")
    return {
        "available": True,
        "status": "descriptive_comparison" if comparison_available else "insufficient_followup",
        "reason": reason,
        "enrollment_date": first_start.isoformat(),
        "elapsed_days": elapsed_days,
        "comparison_available": comparison_available,
        "primary": {
            "role": "가입 후 직접 기록한 수면·기상 규칙성과 주관적 개운함·피로도",
            "first_14_days": primary_first,
            "recent_14_days": primary_recent,
            "descriptive_change_recent_minus_first": (
                {field: (None if primary_first.get(field) is None or primary_recent.get(field) is None or (field == "fatigue_mean" and primary_first.get("subjective_scale_contracts") != primary_recent.get("subjective_scale_contracts")) else round(primary_recent[field] - primary_first[field], 2)) for field in ("bedtime_circular_sd_min", "wake_circular_sd_min", "rested_mean", "fatigue_mean")} if comparison_available else None),
        },
        "auxiliary": auxiliary,
        "sleep_pressure": {
            "measured": False,
            "note": "수면압력은 직접 측정하지 않습니다. 표시되는 S는 수면·각성 이력으로 계산한 모델 상태입니다.",
        },
        "limitations": [
            "가입 전 기록과 archive 자료는 제외합니다.",
            "웨어러블 기록에서 주관적 수면의 질을 생성하지 않습니다.",
            "규칙성·개운함·피로도 변화는 치료 효과 또는 의학적 개선을 증명하지 않습니다.",
            "피로도 척도 계약이 다른 기록은 하나의 평균으로 합치지 않습니다.",
        ],
    }
