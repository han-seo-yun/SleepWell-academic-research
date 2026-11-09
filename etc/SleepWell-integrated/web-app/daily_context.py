"""Validation for context known before a nightly recommendation is generated."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


ZONE = ZoneInfo("Asia/Seoul")
SCHEMA_VERSION = 1
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KINDS = {"work", "class", "first"}


def _local_datetime(value, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}: ISO 시각 문자열이 필요합니다")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field}: ISO 시각 형식이 잘못되었습니다") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZONE)
    return parsed.astimezone(ZONE)


def _number(value, field: str, low: float, high: float) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}: 숫자가 필요합니다")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{field}: {low:g}~{high:g} 범위로 입력해주세요")
    return int(result) if result.is_integer() else result


def _items(payload: dict, key: str) -> list:
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key}: 목록 형식이 필요합니다")
    if len(value) > 12:
        raise ValueError(f"{key}: 최대 12개까지 입력할 수 있습니다")
    return value


def validate_daily_context(payload: dict, now: datetime) -> dict:
    """Return a strict, timezone-normalized snapshot of user-reported context."""
    if not isinstance(payload, dict):
        raise ValueError("일일 일정 입력은 객체 형식이어야 합니다")
    now = now.astimezone(ZONE) if now.tzinfo else now.replace(tzinfo=ZONE)
    night_text = payload.get("night_id")
    if not isinstance(night_text, str) or not _DATE.fullmatch(night_text):
        raise ValueError("night_id: YYYY-MM-DD 형식이 필요합니다")
    try:
        night = date.fromisoformat(night_text)
    except ValueError as error:
        raise ValueError("night_id: 올바른 날짜가 아닙니다") from error
    if abs((night - now.date()).days) > 7:
        raise ValueError("night_id: 오늘 기준 전후 7일 범위로 입력해주세요")

    preparation = _number(payload.get("preparation_min", 0), "preparation_min", 0, 240)
    cutoff = datetime.combine(night, time(20), ZONE)
    schedules = []
    for index, raw in enumerate(_items(payload, "schedules")):
        if not isinstance(raw, dict) or raw.get("kind") not in _KINDS:
            raise ValueError(f"schedules[{index}]: 종류를 확인해주세요")
        start = _local_datetime(raw.get("start"), f"schedules[{index}].start")
        if not cutoff <= start <= cutoff + timedelta(hours=36):
            raise ValueError(f"schedules[{index}].start: 해당 밤 20시부터 36시간 안이어야 합니다")
        end_value = raw.get("end")
        end = None if end_value is None else _local_datetime(end_value, f"schedules[{index}].end")
        if end is not None and (end <= start or end > cutoff + timedelta(hours=48)):
            raise ValueError(f"schedules[{index}].end: 시작 이후이며 기준 시각부터 48시간 안이어야 합니다")
        schedules.append({"kind": raw["kind"], "start": start.isoformat(),
                          "end": end.isoformat() if end else None})
    schedules.sort(key=lambda item: (item["start"], item["kind"], item["end"] or ""))

    event_start = datetime.combine(night, time.min, ZONE)
    event_end = cutoff + timedelta(hours=24)
    naps = []
    for index, raw in enumerate(_items(payload, "naps")):
        if not isinstance(raw, dict):
            raise ValueError(f"naps[{index}]: 객체 형식이 필요합니다")
        start = _local_datetime(raw.get("start"), f"naps[{index}].start")
        end = _local_datetime(raw.get("end"), f"naps[{index}].end")
        duration = (end - start).total_seconds() / 60
        if not event_start <= start < end <= event_end:
            raise ValueError(f"naps[{index}]: 해당 밤의 입력 범위를 벗어났습니다")
        if end > now:
            raise ValueError(f"naps[{index}]: 완료된 낮잠만 입력할 수 있습니다")
        if duration > 240:
            raise ValueError(f"naps[{index}]: 낮잠은 240분 이하여야 합니다")
        naps.append({"start": start.isoformat(), "end": end.isoformat()})
    naps.sort(key=lambda item: item["start"])
    for previous, current in zip(naps, naps[1:]):
        if _local_datetime(current["start"], "nap.start") < _local_datetime(previous["end"], "nap.end"):
            raise ValueError("낮잠 시간이 서로 겹칩니다")

    caffeine = []
    for index, raw in enumerate(_items(payload, "caffeine")):
        if not isinstance(raw, dict):
            raise ValueError(f"caffeine[{index}]: 객체 형식이 필요합니다")
        at = _local_datetime(raw.get("at"), f"caffeine[{index}].at")
        if not event_start <= at <= event_end or at > now:
            raise ValueError(f"caffeine[{index}].at: 해당 밤 범위의 이미 섭취한 시각이어야 합니다")
        caffeine.append({"at": at.isoformat(), "mg": _number(raw.get("mg"), f"caffeine[{index}].mg", 0, 1000)})
    caffeine.sort(key=lambda item: item["at"])

    return {"schema_version": SCHEMA_VERSION, "night_id": night.isoformat(),
            "observed_at": now.isoformat(), "schedules": schedules,
            "preparation_min": preparation, "naps": naps, "caffeine": caffeine}
