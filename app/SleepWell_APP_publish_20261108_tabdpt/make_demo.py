"""SleepWell 합성(가짜) 시연 데이터 생성 — 실제 사람 기록 아님.
수면: 삼성헬스 수면 단계 CSV 형식 (start_time,end_time UTC + time_offset UTC+0900)
설문: 취침 전 웹 설문 'CSV 내보내기' 형식 (participant=Test)
"""
import csv, random, datetime as dt, sys, os
OUT = sys.argv[1]
KST = dt.timedelta(hours=9)
END_DAY = dt.date(2026, 10, 24)   # 마지막 기상일

STAGE = {"AWAKE": 40001, "LIGHT": 40002, "DEEP": 40003, "REM": 40004}

def night_segments(rng, bed, wake):
    """bed~wake(현지) 사이 수면 단계 구간. 90분 주기, 앞쪽 DEEP 많고 뒤쪽 REM 많음, 짧은 각성 섞음"""
    segs = []; t = bed; cycle = 0
    while t < wake:
        plan = [("LIGHT", rng.randint(12, 25)),
                ("DEEP", max(0, rng.randint(25, 40) - cycle * 12)),
                ("LIGHT", rng.randint(15, 30)),
                ("REM", rng.randint(8, 15) + cycle * 6)]
        if rng.random() < 0.45: plan.append(("AWAKE", rng.randint(2, 6)))
        for name, mins in plan:
            if mins <= 0: continue
            e = min(wake, t + dt.timedelta(minutes=mins))
            if e > t: segs.append((t, e, name))
            t = e
            if t >= wake: break
        cycle += 1
    return segs

def write_sleep(path, nights, rng):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["start_time", "end_time", "duration_min", "stage", "stage_name", "sleep_id", "datauuid", "time_offset"])
        n = 0
        for bed, wake, sid in nights:
            for s, e, name in night_segments(rng, bed, wake):
                n += 1
                w.writerow([(s - KST).strftime("%Y-%m-%d %H:%M:%S"), (e - KST).strftime("%Y-%m-%d %H:%M:%S"),
                            int((e - s).total_seconds() // 60), STAGE[name], name, sid, f"SYN-{sid}-{n:04d}", "UTC+0900"])

def at(day, h, m):  # 현지 시각
    return dt.datetime(day.year, day.month, day.day) + dt.timedelta(hours=h, minutes=m)

def scenario(kind, days, seed):
    rng = random.Random(seed)
    nights, surveys = [], []
    for k in range(days, 0, -1):
        wake_day = END_DAY - dt.timedelta(days=k - 1)
        eve = wake_day - dt.timedelta(days=1)          # 그 밤이 시작된 저녁 = 설문 날짜
        weekend = eve.weekday() >= 4                   # 금·토 밤
        if kind == "irregular" and k in (9, 10):        # 이틀 워치 미착용 (기록 없음 구간)
            continue
        if kind == "regular":
            bed = at(eve, 23, 40) + dt.timedelta(minutes=rng.randint(-20, 20))
            wake = at(wake_day, 7, 10) + dt.timedelta(minutes=rng.randint(-15, 15))
            alarm = "07:00"; caffeine = rng.choice([0, 0, 80, 120]); last_caf = "14:00" if caffeine else ""
            nap = 0; ex = rng.choice([0, 30, 40]); fat = rng.randint(2, 4)
        elif kind == "irregular":
            base_h = 1 if not weekend else 3
            bed = at(eve, 23, 0) + dt.timedelta(hours=base_h, minutes=rng.randint(-60, 90))
            wake = at(wake_day, 9 if not weekend else 11, 0) + dt.timedelta(minutes=rng.randint(-90, 60))
            if rng.random() < 0.2: wake = bed + dt.timedelta(hours=rng.uniform(3.5, 4.8))   # 짧게 잔 날
            alarm = "" if weekend else rng.choice(["07:30", "08:00", "09:00"])
            caffeine = rng.choice([120, 150, 240, 300]); last_caf = rng.choice(["16:00", "18:30", "20:00"])
            nap = rng.choice([0, 0, 30, 60, 90]); ex = rng.choice([0, 0, 20]); fat = rng.randint(4, 7)
        else:  # new
            bed = at(eve, 0, 0) + dt.timedelta(days=1, minutes=rng.randint(-40, 30))
            wake = at(wake_day, 8, 0) + dt.timedelta(minutes=rng.randint(-20, 20))
            alarm = "08:00"; caffeine = rng.choice([0, 100]); last_caf = "13:00" if caffeine else ""
            nap = 0; ex = rng.choice([0, 30]); fat = rng.randint(3, 5)
        sid = f"{kind[:3].upper()}{wake_day:%Y%m%d}"
        nights.append((bed, wake, sid))
        # 불규칙: 낮잠도 수면 기록으로
        if kind == "irregular" and nap >= 60:
            ns = at(eve, 15, 30); nights.append((ns, ns + dt.timedelta(minutes=nap), sid + "N"))
        surveys.append({
            "participant": "Test", "date": eve.isoformat(),
            "caffeineMg": caffeine or "", "lastCaffeine": last_caf,
            "napMinutes": nap, "napEnd": (at(eve, 15, 30) + dt.timedelta(minutes=nap)).strftime("%H:%M") if nap else "",
            "exerciseMinutes": ex, "exerciseEnd": "19:00" if ex else "",
            "fatigue": fat, "steps": rng.randint(3000, 11000), "heartRate": rng.randint(68, 88),
            "alarmTime": alarm, "firstCommitment": "09:00" if alarm else "",
            "examTomorrow": "true" if (kind == "irregular" and k % 7 == 0) else "false",
            "alcohol": "true" if (kind == "irregular" and weekend and rng.random() < 0.5) else "false",
            "illness": "false",
            "caffeineUsed": "yes" if caffeine else "no",
            "exerciseIntensity": ("moderate" if ex >= 30 else "light") if ex else "none",
            "savedAt": (at(eve, 21, 0) - KST).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        })
    nights.sort()
    write_sleep(os.path.join(OUT, f"demo_sleep_{kind}.csv"), nights, rng)
    with open(os.path.join(OUT, f"demo_survey_{kind}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(surveys[0].keys())); w.writeheader(); w.writerows(surveys)
    return len(nights), len(surveys)

for kind, days, seed in [("regular", 30, 1), ("irregular", 30, 2), ("new", 5, 3)]:
    print(kind, scenario(kind, days, seed))
