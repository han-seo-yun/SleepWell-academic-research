"""Local recommendation policy, not a clinically validated treatment model.

The target is restorative, regular sleep under a user-selected wake constraint.
Historical behavior forecasts are kept separate from this objective.
"""
from __future__ import annotations

import math
import json
from datetime import datetime, timedelta, time
from statistics import mean, median
from zoneinfo import ZoneInfo

from personalization import load_population_prior, select_population_prior

VERSION = "sleep-improvement-v3-cumulative"
ZONE = ZoneInfo("Asia/Seoul")


def parse_date(value: str) -> datetime:
    d = datetime.fromisoformat(value)
    return d.replace(tzinfo=ZONE) if d.tzinfo is None else d.astimezone(ZONE)


def clock_min(d: datetime) -> float:
    return d.hour * 60 + d.minute + d.second / 60


def clock_value(value: str) -> int:
    h, m = map(int, value.split(":"))
    if not 0 <= h < 24 or not 0 <= m < 60:
        raise ValueError("시각 형식이 잘못되었습니다")
    return h * 60 + m


def bounded(value, low, high):
    return min(high, max(low, value))


def clock_mean(minutes: list[float]) -> float:
    x = sum(math.cos(m * math.tau / 1440) for m in minutes)
    y = sum(math.sin(m * math.tau / 1440) for m in minutes)
    return (math.atan2(y, x) * 1440 / math.tau) % 1440


def clock_delta(a: float, b: float) -> float:
    return (a - b + 720) % 1440 - 720


def advance(s: float, asleep: bool, hours: float) -> float:
    return s * math.exp(-hours / 4.2) if asleep else 1 - (1 - s) * math.exp(-hours / 18.2)


def validate_profile(p: dict) -> dict:
    p = dict(p)
    p["name"] = str(p.get("name", "")).strip()[:60]
    if not p["name"]:
        raise ValueError("이름 또는 별명을 입력해주세요")
    p["age"] = int(p["age"])
    if not 13 <= p["age"] <= 100:
        raise ValueError("현재 프로토타입은 13세 이상을 대상으로 합니다")
    if p.get("occupation") not in ["student", "worker", "other"]:
        raise ValueError("생활 유형을 선택해주세요")
    p["sex"] = p.get("sex", "unspecified")
    if p["sex"] not in ["male", "female", "unspecified"]:
        raise ValueError("성별 항목을 확인해주세요")
    for field in ["usual_bed", "usual_wake", "target_wake", "weekend_wake"]:
        clock_value(p[field])
    for field, low, high in [("usual_sleep_h", 2, 14), ("desired_sleep_h", 7 if p["age"] >= 18 else 8, 9 if p["age"] >= 18 else 10),
                             ("average_steps", 0, 100000), ("exercise_min", 0, 600), ("latency_min", 0, 120)]:
        p[field] = float(p[field])
        if not math.isfinite(p[field]) or not low <= p[field] <= high:
            raise ValueError(f"{field}: {low}~{high} 범위로 입력해주세요")
    activity_hours = p.get("activity_hours_per_day")
    p["activity_hours_per_day"] = None if activity_hours in (None, "") else float(activity_hours)
    if (p["activity_hours_per_day"] is not None and
            (not math.isfinite(p["activity_hours_per_day"]) or not 0 <= p["activity_hours_per_day"] <= 24)):
        raise ValueError("activity_hours_per_day: 0~24 범위로 입력해주세요")
    p["link_history"] = bool(p.get("link_history", False))
    return p


def validate_record(r: dict, now: datetime) -> dict:
    r = dict(r)
    bed, wake = parse_date(r["bed"]), parse_date(r["wake"])
    hours = (wake - bed).total_seconds() / 3600
    if wake > now or not 0.5 <= hours <= 16:
        raise ValueError("완료된 수면을 입력해주세요. 취침~기상 간격은 30분~16시간이어야 합니다")
    r["bed"], r["wake"] = bed.isoformat(), wake.isoformat()
    r["date"] = wake.date().isoformat()
    for field, low, high in [("latency_min", 0, 180), ("awake_min", 0, 600), ("fatigue", 0, 10),
                             ("rested", 1, 5), ("steps", 0, 100000), ("exercise_min", 0, 600)]:
        value = r.get(field)
        r[field] = None if value is None or value == "" else float(value)
        if r[field] is not None and (not math.isfinite(r[field]) or not low <= r[field] <= high):
            raise ValueError(f"{field}: {low}~{high} 범위로 입력해주세요")
    lost = (r["latency_min"] or 0) + (r["awake_min"] or 0)
    if lost >= hours * 60:
        raise ValueError("잠들기까지의 시간과 밤중 각성의 합이 침대에 있던 시간보다 큽니다")
    r["sleep_min"] = hours * 60 - lost
    r["note"] = str(r.get("note", ""))[:1000]
    r["source"] = "diary"
    r["observed_at"] = now.isoformat()
    return r


def _cumulative_records(profile: dict, records: list[dict], now: datetime) -> tuple[list[dict], int]:
    """Return one latest, observable record per wake date since enrollment."""
    enrolled = parse_date(profile["created_at"])
    latest: dict[str, tuple[datetime, str, dict, bool]] = {}
    for record in records:
        if record.get("source") not in ("diary", "healthkit"):
            continue
        try:
            wake = parse_date(record["wake"])
            if not enrolled <= wake <= now:
                continue
            legacy = not record.get("observed_at")
            observed_at = wake if legacy else parse_date(record["observed_at"])
            if observed_at > now:
                continue
            day = wake.date().isoformat()
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
        candidate_key = (observed_at, canonical)
        previous = latest.get(day)
        if previous is None or candidate_key > previous[:2]:
            latest[day] = (observed_at, canonical, record, legacy)
    ordered = sorted(latest.values(), key=lambda item: (item[0], item[1]))
    return [item[2] for item in ordered], sum(item[3] for item in ordered)


def parameters(profile: dict, records: list[dict], now: datetime, prior: dict | None = None,
               population_prior: dict | None = None) -> dict:
    enrolled = parse_date(profile["created_at"])
    elapsed = max(0, (now.date() - enrolled.date()).days)
    eligible, legacy_days = _cumulative_records(profile, records, now)
    n = len(eligible)
    active = elapsed >= 14 and n >= 14
    base_bed, base_wake = clock_value(profile["usual_bed"]), clock_value(profile["usual_wake"])
    base_phase = (base_bed + ((base_wake - base_bed) % 1440) / 2) % 1440
    declared_target = profile["desired_sleep_h"] * 60
    low, high = (480, 600) if profile["age"] < 18 else (420, 540)
    loaded = population_prior if population_prior is not None else load_population_prior()
    if loaded.get("artifact_type") == "sleepwell_population_prior":
        loaded = {"available": True, "artifact": loaded, "reason": "사용 가능", "source": None}
    selected_prior = select_population_prior(profile, loaded)
    sleep_target = declared_target
    if selected_prior.get("available"):
        sleep_target = bounded(selected_prior["mean_sleep_min"], low, high)
    initial_sleep_target = sleep_target
    phase = base_phase
    latency, activity, exercise = profile["latency_min"], profile["average_steps"], profile["exercise_min"]
    origins = {"phase": "가입 시 평소 취침·기상", "sleep_target": "가입 시 희망 수면시간",
               "activity": "가입 시 활동 평균", "exercise": "가입 시 운동 평균"}
    if selected_prior.get("available"):
        origins["sleep_target"] = (f"{selected_prior['level']} 집단 {selected_prior['n_people']}명의 관측 수면시간 평균 "
                                   "(개인의 최적 필요 수면시간을 뜻하지 않음)")
    # The linked archive is a personal observed prior, never a demographic population average.
    if profile["link_history"] and prior and prior.get("average_steps") is not None:
        activity = prior["average_steps"]; origins["activity"] = "연결한 본인 과거 데이터 평균"
    if profile["link_history"] and prior and prior.get("recent_phase_min") is not None:
        # Historical records can be stale: retain most of the current self-report.
        base_phase = (base_phase + 0.25 * clock_delta(prior["recent_phase_min"], base_phase)) % 1440
        phase = base_phase
        origins["phase"] = "현재 입력 75% + 본인 과거 최근 30회 수면 중간시각 25%"
    alpha = n / (n + 14) if active else 0.0
    if active:
        # Midpoint on absolute time handles midnight correctly.
        midpoints = [clock_min(parse_date(r["bed"]) + timedelta(seconds=(parse_date(r["wake"]) - parse_date(r["bed"])).total_seconds() / 2)) for r in eligible]
        phase = (base_phase + alpha * clock_delta(clock_mean(midpoints), base_phase)) % 1440
        origins["phase"] = f"가입 후 누적 {n}일 관측 + 초기값 수축"
        good = [r["sleep_min"] for r in eligible if r.get("rested") is not None and r["rested"] >= 4 and (r.get("fatigue") is None or r["fatigue"] <= 3)]
        if len(good) >= 5:
            sleep_target = bounded((1 - alpha) * initial_sleep_target + alpha * median(good),
                                   max(low, initial_sleep_target - 30), min(high, initial_sleep_target + 30))
            origins["sleep_target"] = f"가입 후 누적 개운한 날 {len(good)}일 + 초기 기준 (초기 기준값 ±30분 제한)"
        for key, field in [("latency", "latency_min"), ("activity", "steps"), ("exercise", "exercise_min")]:
            values = [r[field] for r in eligible if r.get(field) is not None]
            if values:
                observed = mean(values)
                if key == "latency": latency = (1 - alpha) * latency + alpha * observed
                if key == "activity": activity = (1 - alpha) * activity + alpha * observed
                if key == "exercise": exercise = (1 - alpha) * exercise + alpha * observed
                origins[key] = f"가입 후 누적 관측 {len(values)}일 + 초기 평균"
    compatibility_note = (f" observed_at이 없는 기존 기록 {legacy_days}일은 호환 목적으로 기상 시각에 관측된 것으로 처리했으며 연구 모델 학습 자료라는 뜻은 아닙니다."
                          if legacy_days else "")
    return {"mode": "personalized" if active else "initial", "elapsed_days": elapsed, "valid_days": n,
            "required_days": 14, "cumulative_valid_days": n, "personalization_weight": alpha,
            "legacy_compatible_days": legacy_days, "phase_min": phase, "target_sleep_min": sleep_target, "latency_min": latency,
            "average_steps": activity, "exercise_min": exercise, "tau_wake_h": 18.2, "tau_sleep_h": 4.2,
            "circadian_amplitude": 0.15, "origins": origins,
            "population_prior": {"available": bool(selected_prior.get("available")),
                                 "version": selected_prior.get("version"), "n_people": selected_prior.get("n_people"),
                                 "source": selected_prior.get("source"), "level": selected_prior.get("level"),
                                 "reason": selected_prior.get("reason")},
            "note": ("τw·τs는 수면 기록만으로 안정적인 식별이 어려워 18.2시간/4.2시간으로 고정합니다. "
                     "개인 기록은 가입 후 누적되며 위상·수면 목표·입면시간·활동 기준을 갱신합니다." + compatibility_note)}


def recommend(profile: dict, records: list[dict], now: datetime, prior: dict | None = None,
              population_prior: dict | None = None) -> dict:
    params = parameters(profile, records, now, prior, population_prior)
    # Tonight's plan; wake constraint belongs to the following morning.
    # Before the usual morning wake time, plan the remainder of this night.
    today_wake_clock = profile["weekend_wake"] if now.weekday() >= 5 else profile["target_wake"]
    before_morning = clock_min(now) < min(12 * 60, clock_value(today_wake_clock))
    tomorrow = now.date() if before_morning else now.date() + timedelta(days=1)
    wake_clock = profile["weekend_wake"] if tomorrow.weekday() >= 5 else profile["target_wake"]
    wake = datetime.combine(tomorrow, time.fromisoformat(wake_clock), ZONE)
    recent = sorted([r for r in records if parse_date(r["wake"]) <= now and now - parse_date(r["wake"]) <= timedelta(days=7)], key=lambda r: r["wake"])
    observed_sleep = mean([r["sleep_min"] for r in recent]) if recent else None
    baseline_sleep = observed_sleep if observed_sleep is not None else profile["usual_sleep_h"] * 60
    debt = max(0, params["target_sleep_min"] - baseline_sleep)
    # Explicit conservative policy, not a learned medical dose-response.
    extension = min(30, debt * 0.25)
    desired = params["target_sleep_min"] + extension
    latest_bed = wake - timedelta(minutes=desired + params["latency_min"])
    start = now - timedelta(days=7)
    s = 0.5
    cursor = start
    intervals = [(parse_date(r["bed"]) + timedelta(minutes=r.get("latency_min") or 0), parse_date(r["wake"])) for r in recent]
    observed_dates = {parse_date(r["wake"]).date() for r in recent}
    while cursor < now:
        next_time = min(now, cursor + timedelta(minutes=5))
        asleep = any(a <= cursor < b for a, b in intervals)
        # Missing diary days are unknown; use declared routine rather than seven days awake.
        expected_wake_date = cursor.date() + timedelta(days=1) if clock_min(cursor) >= clock_value(profile["usual_bed"]) and clock_value(profile["usual_bed"]) > clock_value(profile["usual_wake"]) else cursor.date()
        if not asleep and expected_wake_date not in observed_dates:
            minute = clock_min(cursor)
            b, w = clock_value(profile["usual_bed"]), clock_value(profile["usual_wake"])
            asleep = ((minute - b) % 1440) < ((w - b) % 1440)
        s = advance(s, asleep, (next_time - cursor).total_seconds() / 3600)
        cursor = next_time
    current_s = s
    chart = []
    for i in range(145):
        t = now + timedelta(minutes=10 * i)
        if i:
            s = advance(s, False, 1 / 6)
        c = 0.15 * math.cos(math.tau * (clock_min(t) - params["phase_min"]) / 1440)
        chart.append({"time": t.isoformat(), "s": s, "c": c, "propensity": s + c})
    # Optimize only near a schedule that preserves sleep opportunity.
    candidates = []
    for offset in range(-30, 1, 5):
        candidate = latest_bed + timedelta(minutes=offset)
        if candidate < now:
            continue
        cs = advance(current_s, False, (candidate - now).total_seconds() / 3600)
        cc = 0.15 * math.cos(math.tau * (clock_min(candidate) - params["phase_min"]) / 1440)
        preferred_bed = (params["phase_min"] - params["target_sleep_min"] / 2) % 1440 if params["mode"] == "personalized" else clock_value(profile["usual_bed"])
        regularity = abs(clock_delta(clock_min(candidate), preferred_bed)) / 60
        score = (cs + cc) - 0.08 * regularity - abs(offset) / 3000
        candidates.append((score, candidate))
    bed = max(candidates, key=lambda pair: pair[0])[1] if candidates else now
    feasible = bed <= latest_bed
    warnings = []
    if not feasible: warnings.append("오늘은 목표 기상까지 필요한 수면 기회를 모두 확보하기 어렵습니다. 기상 일정 조정 가능 여부를 확인하세요.")
    if not recent: warnings.append("최근 7일 기록이 없어 평소 취침·기상 패턴으로 수면압력을 초기화했습니다.")
    else: warnings.append("기록 없는 날은 평소 수면 패턴으로 보완합니다. 낮잠·밤중 각성의 실제 시각은 미반영입니다.")
    shift = abs(clock_delta(clock_min(bed), clock_value(profile["usual_bed"])))
    if shift > 90: warnings.append("평소 취침과 90분 이상 차이가 납니다. 목표 기상과 수면시간 설정을 함께 확인하세요.")
    reasons = [f"다음날 기상 일정 {wake_clock}을 우선 반영했습니다.",
               f"수면 목표 {params['target_sleep_min'] / 60:.1f}시간과 입면 여유 {params['latency_min']:.0f}분을 확보합니다.",
               f"최근 부족분을 반영한 추가 수면 기회는 {extension:.0f}분입니다 (최대 30분의 초기 운영 규칙).",
               "Two-Process 수면압력과 일주기 리듬으로 확보 가능한 취침 구간 안에서 시각을 비교했습니다."]
    return {"policy_version": VERSION, "objective": "수면 개선을 위한 권장 취침·기상 시각", "generated_at": now.isoformat(),
            "plan_date": now.date().isoformat(), "bed": bed.isoformat(), "wake": wake.isoformat(),
            "wind_down": (bed - timedelta(minutes=30)).isoformat(), "sleep_opportunity_min": max(0, (wake - bed).total_seconds() / 60),
            "target_sleep_min": desired, "feasible": feasible, "parameters": params, "reasons": reasons, "warnings": warnings,
            "chart": chart, "current_s": current_s, "observed_sleep_min": observed_sleep,
            "evidence": "추천 규칙은 아직 수면 개선 효과를 검증하지 않은 연구용 정책입니다."}
