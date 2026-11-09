"""
실험 2 — 실제 워치 수면 기록으로 PCR 모델 예측 검증 (신혜지 본인 데이터)

질문: 저녁 8시까지의 실제 수면·각성 이력을 모델에 넣으면, 그날 밤 취침 시각과
      다음 날 기상 시각을 얼마나 맞히는가? 단순 기준선보다 나은가?
      (팀 미래예측모델 causal_sleep_timing_forecast.py 와 같은 20:00 예측 시점)

데이터: com.samsung.shealth.sleep.20261025094402.csv (2026-09-13 ~ 2026-10-18 사용)
  - 시각 해석: 파일 시각 = UTC 로 보고 +9h (KST). 근거는 inspect_sleep.py 출력 참고
  - 60분 이내로 붙은 세션은 하나로 합침. 3시간 이상 = 주 수면, 미만 = 낮잠/조각잠

모델 구동
  1) 워밍업: 첫 3밤의 중앙 일정으로 21일 돌려 생체시계를 맞춘다 (이 3밤은 평가 제외)
  2) 이력 재현: 실제로 잔 구간은 강제 수면(W=-4), 깨어 있던 구간은 강제 각성(W=+4).
     기록이 없는 밤은 그 시점까지의 과거 주 수면 중앙값으로 채운다 (미래 정보 사용 안 함)
  3) 예측: 평가 밤마다 20:00 상태에서 강제 항을 끄고 20시간 자유롭게 돌린다.
     처음 1시간 이상 이어지는 잠 = 예측 취침, 그 잠의 끝 = 예측 기상
  빛 가정: 깨어 있을 때 250 lux(실내), 잘 때 0. 자유 실행 중에는 250 lux(눈 감으면 3%)
  민감도: 낮(07~18시) 각성 중 1000 lux

비교 대상 (모두 20:00 이전 정보만 사용)
  - 전날과 같음: 직전에 기록된 주 수면의 시각
  - 최근 7밤 중앙값
  - PCR: 모델 예측 그대로
  - PCR+개인보정: 이전 평가 밤들의 평균 오차만큼 빼줌 (팀의 '잔차 보정' 아이디어)
"""
import json, sys
import numpy as np, pandas as pd
import pcr, pcr_ext as pe

W_F = 4.0
DAY_LUX = float(sys.argv[1]) if len(sys.argv) > 1 else 250.0   # 낮 07~18시 각성 중 조도
NIGHT_LUX = 250.0
TAG = f"day{int(DAY_LUX)}"

# ── 1. 데이터 ────────────────────────────────────────────────────────────
df = pd.read_csv("realdata/sleep.csv", skiprows=1, index_col=False)
P = "com.samsung.health.sleep."
st = pd.to_datetime(df[P + "start_time"]) + pd.Timedelta(hours=9)
en = pd.to_datetime(df[P + "end_time"]) + pd.Timedelta(hours=9)
ses = pd.DataFrame({"s": st, "e": en}).sort_values("s")
ses = ses[(ses.s >= "2026-09-13") & (ses.e > ses.s)].reset_index(drop=True)

eps = []                                   # 60분 이내 간격 병합
for s, e in zip(ses.s, ses.e):
    if eps and (s - eps[-1][1]) <= pd.Timedelta(minutes=60):
        eps[-1][1] = max(eps[-1][1], e)
    else:
        eps.append([s, e])
eps = pd.DataFrame(eps, columns=["s", "e"])
eps["dur_h"] = (eps.e - eps.s).dt.total_seconds() / 3600
eps["main"] = eps.dur_h >= 3
# 저녁 날짜: 취침이 정오 이전이면 전날 저녁에 속함
eps["evening"] = (eps.s - pd.Timedelta(hours=12)).dt.normalize()

main = eps[eps.main].drop_duplicates("evening", keep="first").set_index("evening")
T0 = main.index.min()                                  # 첫 저녁
evenings = pd.date_range(T0, main.index.max(), freq="D")

def hrs_after_cutoff(ts, ev):                          # 그 저녁 20:00 기준 경과시간
    return (ts - (ev + pd.Timedelta(hours=20))).total_seconds() / 3600

# ── 2. 강제 일정(과거 정보만으로 빈 밤 채우기) ─────────────────────────────
sched = []                                             # (start, end, kind)
filled = {}
for ev in evenings:
    if ev in main.index:
        r = main.loc[ev]; sched.append((r.s, r.e, "actual")); filled[ev] = False
    else:
        past = main[main.index < ev]
        on = np.median([hrs_after_cutoff(x, i) for i, x in zip(past.index, past.s)])
        off = np.median([hrs_after_cutoff(x, i) for i, x in zip(past.index, past.e)])
        c = ev + pd.Timedelta(hours=20)
        sched.append((c + pd.Timedelta(hours=on), c + pd.Timedelta(hours=off), "imputed")); filled[ev] = True
for _, r in eps[~eps.main].iterrows():                 # 낮잠/조각잠도 실제 수면으로
    sched.append((r.s, r.e, "nap"))

# 워밍업: 첫 3밤 중앙 일정 21일
first3 = main.iloc[:3]
on0 = np.median([hrs_after_cutoff(x, i) for i, x in zip(first3.index, first3.s)])
off0 = np.median([hrs_after_cutoff(x, i) for i, x in zip(first3.index, first3.e)])
ORIGIN = T0 - pd.Timedelta(days=21)
for k in range(21):
    c = ORIGIN + pd.Timedelta(days=k, hours=20)
    sched.append((c + pd.Timedelta(hours=on0), c + pd.Timedelta(hours=off0), "spinup"))

def to_h(ts):
    return (ts - ORIGIN).total_seconds() / 3600

T_END = to_h(evenings[-1] + pd.Timedelta(days=1, hours=16))
DT = 1 / 60
grid = np.arange(0, T_END + DT, DT)
asleep_sched = np.zeros_like(grid, dtype=bool)
for s, e, _ in sched:
    asleep_sched[(grid >= to_h(s)) & (grid < to_h(e))] = True

hod = (grid + ORIGIN.hour) % 24
lux_hist = np.where(asleep_sched, 0.0,
                    np.where((hod >= 7) & (hod < 18), DAY_LUX, NIGHT_LUX))
L_hist = pcr.make_interp_light(grid, lux_hist)
W_hist = lambda t: -W_F if asleep_sched[min(int(round(t / DT)), len(grid) - 1)] else W_F

sol = pe.simulate_forced(T_END, L_hist, W_hist, dt=DT)
check_m = grid > 21 * 24
agree = (pcr.is_asleep(sol.y[4]) == asleep_sched)[check_m].mean()

# ── 3. 평가 밤마다 20:00 에서 자유 실행 ───────────────────────────────────
targets = [ev for ev in main.index[3:]]
rows = []
for ev in targets:
    tc = to_h(ev + pd.Timedelta(hours=20))
    i0 = int(round(tc / DT))
    V0 = list(sol.y[:, i0])
    g = np.arange(0, 20 + DT, DT)
    hh = (g + tc + ORIGIN.hour) % 24
    lux_f = np.where((hh >= 7) & (hh < 18), DAY_LUX, NIGHT_LUX)
    fr = pe.simulate_forced(20, pcr.make_interp_light(g, lux_f), lambda t: 0.0, V0=V0, dt=DT)
    a = pcr.is_asleep(fr.y[4])
    # 1시간 이상 이어지는 첫 잠, 30분 미만 각성은 무시
    on_i = off_i = None
    k = 0
    while k < len(a):
        if a[k]:
            j = k
            while j < len(a):
                if a[j]:
                    j += 1; continue
                gap = j
                while gap < len(a) and not a[gap]:
                    gap += 1
                if gap - j < 30 and gap < len(a):
                    j = gap; continue
                break
            if (j - k) * DT >= 1:
                on_i, off_i = k, j; break
            k = j
        k += 1
    r = main.loc[ev]
    past = main[main.index < ev]
    prev = past.iloc[-1]
    last7 = past.iloc[-7:]
    rows.append(dict(
        evening=str(ev.date()), prev_night_imputed=bool(filled.get(ev - pd.Timedelta(days=1), False)),
        H_at_20=float(sol.y[5][i0]),
        actual_on=hrs_after_cutoff(r.s, ev), actual_off=hrs_after_cutoff(r.e, ev),
        pcr_on=on_i * DT if on_i is not None else np.nan, pcr_off=off_i * DT if off_i is not None else np.nan,
        prev_on=hrs_after_cutoff(prev.s, past.index[-1]), prev_off=hrs_after_cutoff(prev.e, past.index[-1]),
        med7_on=float(np.median([hrs_after_cutoff(x, i) for i, x in zip(last7.index, last7.s)])),
        med7_off=float(np.median([hrs_after_cutoff(x, i) for i, x in zip(last7.index, last7.e)])),
    ))
R = pd.DataFrame(rows)
for k in ["on", "off"]:                                 # 개인보정: 과거 평균 오차 빼기 (첫 밤은 보정 없음)
    err = R[f"pcr_{k}"] - R[f"actual_{k}"]
    bias = err.expanding().mean().shift(1).fillna(0)
    R[f"pcrcal_{k}"] = R[f"pcr_{k}"] - bias

def mae(col, k):
    return float(np.nanmean(np.abs(R[f"{col}_{k}"] - R[f"actual_{k}"])) * 60)
def bias(col, k):
    return float(np.nanmean(R[f"{col}_{k}"] - R[f"actual_{k}"]) * 60)

summary = {}
print(f"\n[{TAG}] forced-history agreement with record: {agree:.4f}   eval nights n={len(R)}  "
      f"(prev night imputed: {int(R.prev_night_imputed.sum())})")
for col, name in [("prev", "전날과 같음"), ("med7", "최근 7밤 중앙값"), ("pcr", "PCR"), ("pcrcal", "PCR+개인보정")]:
    summary[col] = dict(name=name, on_mae_min=round(mae(col, "on"), 1), off_mae_min=round(mae(col, "off"), 1),
                        on_bias_min=round(bias(col, "on"), 1), off_bias_min=round(bias(col, "off"), 1))
    print(f"  {name:12s} 취침 MAE {summary[col]['on_mae_min']:6.1f}분 (편향 {summary[col]['on_bias_min']:+6.1f})   "
          f"기상 MAE {summary[col]['off_mae_min']:6.1f}분 (편향 {summary[col]['off_bias_min']:+6.1f})")

def clock(h):
    m = int(round(((20 + h) % 24) * 60)); return f"{m // 60:02d}:{m % 60:02d}"
print("\n  저녁        실제취침 PCR취침 | 실제기상 PCR기상 | 전날빈칸")
for _, r in R.iterrows():
    print(f"  {r.evening}  {clock(r.actual_on)}  {clock(r.pcr_on)} | {clock(r.actual_off)}  {clock(r.pcr_off)} | {r.prev_night_imputed}")

out = dict(day_lux=DAY_LUX, n=len(R), forced_history_agreement=round(float(agree), 4),
           n_evenings_total=len(evenings), n_imputed_evenings=int(sum(filled.values())),
           summary=summary, nights=R.round(3).to_dict(orient="records"))
json.dump(out, open(f"results_realdata_{TAG}.json", "w"), ensure_ascii=False, indent=1)
