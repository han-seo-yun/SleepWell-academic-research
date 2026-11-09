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


def _descriptive_changes(first: dict, recent: dict) -> dict:
    result = {}
    for field in ("bedtime_circular_sd_min", "wake_circular_sd_min", "rested_mean", "fatigue_mean"):
        before, after = first.get(field), recent.get(field)
        if field == "fatigue_mean" and first.get("subjective_scale_contracts") != recent.get("subjective_scale_contracts"):
            result[field] = None
            continue
        result[field] = None if before is None or after is None else round(after - before, 2)
    return result


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
                _descriptive_changes(primary_first, primary_recent) if comparison_available else None),
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
