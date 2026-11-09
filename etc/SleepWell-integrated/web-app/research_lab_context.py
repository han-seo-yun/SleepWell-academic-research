"""Cutoff-aware, optional daily context for frozen-model diagnostics."""
from collections import Counter
from datetime import datetime, timedelta
import math

from night_core import ZONE

MAX_EVENTS = 10000
KINDS = ("steps", "exercise", "caffeine", "schedule", "nap")
FIELDS = ("steps", "exercise_min", "caffeine_mg", "schedule_start_offset_min", "nap_min")


def instant(value):
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("생활 정보의 시각은 시간대를 포함한 ISO 문자열이어야 합니다.")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("생활 정보의 시각에는 시간대가 필요합니다.")
    return result.astimezone(ZONE)


def normalize_context(events, participants):
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError(f"context는 {MAX_EVENTS}건 이하의 이벤트 배열이어야 합니다.")
    result, seen = [], set()
    for raw in events:
        if not isinstance(raw, dict) or set(raw) != {
            "participant_id", "kind", "start", "end", "value", "observed_at"
        }:
            raise ValueError("context 필드: participant_id, kind, start, end, value, observed_at")
        participant, kind = raw["participant_id"], raw["kind"]
        if not isinstance(participant, str) or participant not in participants or kind not in KINDS:
            raise ValueError("생활 정보의 참가자 또는 종류를 확인해주세요.")
        start, observed = instant(raw["start"]), instant(raw["observed_at"])
        end = instant(raw["end"]) if raw["end"] is not None else start
        if end < start or end - start > timedelta(days=2):
            raise ValueError("생활 정보 구간은 시작 이후, 48시간 이내여야 합니다.")
        if kind != "schedule" and end > observed:
            raise ValueError("완료된 생활 정보만 관측 시각에 사용할 수 있습니다.")
        value = raw["value"]
        if kind in ("steps", "caffeine"):
            maximum = 200000 if kind == "steps" else 3000
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError("걸음 수·카페인 양을 확인해주세요.")
            if kind == "steps" and value != int(value):
                raise ValueError("걸음 수는 정수여야 합니다.")
        elif value is not None:
            raise ValueError("운동·일정·낮잠은 start/end로 표현하고 value는 null로 입력하세요.")
        if kind in ("exercise", "nap", "steps") and start == end:
            raise ValueError("활동 구간에는 시작과 종료 시각이 필요합니다.")
        row = dict(participant_id=participant, kind=kind, start=start.isoformat(),
                   end=end.isoformat(), value=value, observed_at=observed.isoformat())
        key = tuple(row.values())
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return sorted(result, key=lambda x: (x["participant_id"], x["start"], x["kind"], x["observed_at"]))


def context_features(rows, events):
    parsed = [(x, instant(x["start"]), instant(x["end"]), instant(x["observed_at"])) for x in events]
    features = []
    for row in rows:
        cutoff = row["cutoff"]
        midnight = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
        values = {key: None for key in FIELDS}
        counts = Counter()
        excluded = 0
        for event, start, end, observed in parsed:
            if event["participant_id"] != row["participant_id"]:
                continue
            kind = event["kind"]
            in_window = (cutoff <= start <= cutoff + timedelta(hours=36)) if kind == "schedule" else (midnight <= start < cutoff)
            if not in_window:
                continue
            if observed >= cutoff or (kind != "schedule" and end >= cutoff):
                excluded += 1
                continue
            counts[kind] += 1
            if kind == "schedule":
                offset = (start - cutoff).total_seconds() / 60
                previous = values["schedule_start_offset_min"]
                values["schedule_start_offset_min"] = offset if previous is None else min(offset, previous)
            else:
                field = {"steps": "steps", "exercise": "exercise_min", "caffeine": "caffeine_mg", "nap": "nap_min"}[kind]
                amount = event["value"] if kind in ("steps", "caffeine") else (end - start).total_seconds() / 60
                values[field] = (values[field] or 0) + amount
        features.append({"participant_id": row["participant_id"], "cutoff": cutoff.isoformat(),
                         **values, "event_counts": dict(counts), "unavailable_events": excluded})
    counts = Counter(x["kind"] for x in events)
    summary = {
        "events": len(events), "coverage": {kind: counts[kind] for kind in KINDS},
        "available_nights": {field: sum(x[field] is not None for x in features) for field in FIELDS},
        "model_input_status": "diagnostic_only_frozen_models_unchanged",
        "note": "20시 전에 관측된 완료 활동과 당시 알려진 향후 일정을 집계합니다. 미관측은 0으로 채우지 않습니다. 동결 예측모델의 추가 입력은 아닙니다.",
    }
    return features, summary
