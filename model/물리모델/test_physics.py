"""
물리모델 스모크 테스트 + 민감도 분석.

★ 이 파일은 '회귀 게이트'가 아니다. 회귀 게이트는 validate/compare.py 다.
  뮤테이션 테스트(2026-09-02) 결과, 느슨한 범위 판정만으로는
  dVdt(4) 의 1/2 계수 제거·k 누락·눈꺼풀 게이트 제거가 전부 통과했다.
  그래서 여기서는 (1) 정상상태 값을 좁은 허용오차로 못박고,
  (2) 게이트를 실제로 자극하는 시나리오를 따로 둔다.
"""
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import circadian as C
import pcr

ok = True
def check(label, cond, detail):
    global ok
    ok &= bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}: {detail}")

def hhmm(h):
    h = h % 24
    return f"{int(h):02d}:{round(h % 1 * 60):02d}"


# ══════════════════════════════════════════════════════════════════
print("A. circadian.py — simpler.m (일주기 3변수)")
DAYS = 12
light = C.sleep_wake_light([d*24 for d in range(DAYS+1)],      # 자정 취침
                           [d*24+8 for d in range(DAYS+1)],    # 08시 기상
                           DAYS*24, dt=1/60)
t, x, y, n = C.simulate(light=light, t_span=(0, DAYS*24), dt=1/60)

def day(v, d):
    return v[(t >= d*24) & (t < (d+1)*24)]

d1, d10 = day(x, 1), day(x, 10)
L = min(len(d1), len(d10))
gap = np.abs(d1[:L] - d10[:L]).max()
check("한계주기 수렴", gap < 0.05, f"1일차 vs 10일차 최대 절대차 {gap:.5f} (< 0.05)")

# ★ 진짜 극소점 간격. 창 argmin 이 아니므로 항진명제가 아니다.
mins = C.daily_minima(t, x, day0=3)
per = np.diff(mins)
check("주기 24시간", per.size >= 5 and abs(per.mean() - 24) < 0.02,
      f"극소점 {mins.size}개, 간격 평균 {per.mean():.4f} h (±0.02)")

# ★ CBTmin = min(x). 근거: FJK1999 식(10) phi_ref=0 / IC_mean_midsleep_method.m L31
cbt = C.daily_minima(t, x, day0=8, day1=12) % 24
check("CBTmin 값 고정", cbt.size == 4 and abs(cbt.mean() - 5.58) < 0.05 and cbt.std() < 0.02,
      f"{[hhmm(c) for c in cbt]} (기대 05:35 ±3분)")

cbt_y = C.daily_minima(t, y, day0=8, day1=12) % 24
check("min(y) 는 CBTmin 이 아님", abs((cbt.mean() - cbt_y.mean()) % 24 - 7.07) < 0.3,
      f"min(y)={[hhmm(c) for c in cbt_y]} — min(x) 와 약 7.07h(위상 90°) 차이")

# dt 를 바꿔도 CBTmin 이 흔들리지 않아야 한다 (구간경계 상태인계 회귀 방지)
c5 = C.daily_minima(*C.simulate(light=C.sleep_wake_light(
        [d*24 for d in range(DAYS+1)], [d*24+8 for d in range(DAYS+1)], DAYS*24, dt=5/60),
        t_span=(0, DAYS*24), dt=5/60)[:2], day0=8, day1=12) % 24
# dt 는 조도 격자 해상도이기도 하므로 전이 램프 폭이 실제로 달라진다.
# 구간경계 상태인계 버그가 있던 때는 21분까지 벌어졌다 -> 5분 이내면 정상.
check("dt 비의존성", abs(c5.mean() - cbt.mean()) < 0.09,
      f"dt=1분 {hhmm(cbt.mean())} vs dt=5분 {hhmm(c5.mean())} (차 {abs(c5.mean()-cbt.mean())*60:.1f}분, 버그 시 21분)")


# ══════════════════════════════════════════════════════════════════
print("\nB. pcr.py — PCR.m (6변수)")
sol = pcr.simulate(24*40)
tp, xp, H = sol.t, sol.y[1], sol.y[5]
ep = pcr.sleep_episodes(tp, sol.y[4])
check("수면 에피소드 존재", len(ep) >= 35, f"40일간 {len(ep)}개")
if not ep:
    print("\n" + "="*46); print("전체 결과: 실패 ❌ (수면 없음)"); sys.exit(1)

dur = np.array([b - a for a, b in ep[-5:]])
check("수면 길이", abs(dur.mean() - 8.13) < 0.05, f"최근 5일 평균 {dur.mean():.3f} h (기대 8.13 ±0.05)")
check("취침 시각", abs(ep[-1][0] % 24 - 22.07) < 0.05, f"{hhmm(ep[-1][0])} (기대 22:04 ±3분)")
check("기상 시각", abs(ep[-1][1] % 24 - 6.20) < 0.05, f"{hhmm(ep[-1][1])} (기대 06:12 ±3분)")

m = (tp >= 24*38) & (tp < 24*39)
cbt_p = tp[m][np.argmin(xp[m])] % 24
lag = (ep[-1][1] % 24 - cbt_p) % 24
check("CBTmin 값", abs(cbt_p - 3.14) < 0.08, f"{hhmm(cbt_p)} (기대 03:08 ±5분)")
check("기상은 CBTmin 이후", 1 <= lag <= 4, f"{lag:.2f} h 뒤")

h_on  = H[np.argmin(abs(tp - ep[-1][0]))]
h_off = H[np.argmin(abs(tp - ep[-1][1]))]
check("수면압력 부호", h_on > h_off, f"취침시 {h_on:.2f} > 기상시 {h_off:.2f}")


# ══════════════════════════════════════════════════════════════════
print("\nC. 눈꺼풀 게이트 — 게이트를 실제로 자극하는 시나리오")
# 기준 조명(06:00~21:42)은 수면(22:04~06:12)과 겹침이 23분뿐이라 게이트를 거의 안 건드린다.
# 24시간 상시 조명이면 수면 구간 전체가 게이트를 통과하므로 여기서 검출된다.
_tg = np.arange(0, 24*40 + 0.1, 2/60)
always = pcr.make_interp_light(_tg, np.full_like(_tg, 250.0))

def _run(gate):
    s = pcr.simulate(24*40, light_fn=always, gate=gate)
    e = pcr.sleep_episodes(s.t, s.y[4])
    if not e:
        return float('nan'), float('nan'), 0
    return e[-1][0] % 24, float(np.mean([b-a for a, b in e[-5:]])), len(e)

o_on,  d_on,  n_on  = _run(1)   # 눈꺼풀 3%
o_off, d_off, n_off = _run(0)   # 하드 게이팅(수면 중 빛 0)
check("게이트가 결과를 바꾼다",
      (n_on and n_off) and (abs(o_on - o_off) > 0.1 or abs(d_on - d_off) > 0.1),
      f"상시 250lux — gate=1 취침 {hhmm(o_on)}/{d_on:.2f}h  vs  gate=0 취침 {hhmm(o_off)}/{d_off:.2f}h")


# ══════════════════════════════════════════════════════════════════
print("\nD. 민감도 분석 (판정 아님 — 발표 대응용 수치)")
base = pcr.simulate(24*60)
be = pcr.sleep_episodes(base.t, base.y[4])
lit = pcr.simulate(24*60, **pcr.LITERATURE_PARAMS)
le = pcr.sleep_episodes(lit.t, lit.y[4])
def summ(s, e, tag):
    m = (s.t >= 24*58) & (s.t < 24*59)
    c = s.t[m][np.argmin(s.y[1][m])] % 24
    print(f"  {tag:26s} 취침 {hhmm(e[-1][0])}  기상 {hhmm(e[-1][1])}  "
          f"길이 {e[-1][1]-e[-1][0]:.2f}h  CBTmin {hhmm(c)}")
    return e[-1][0] % 24, c
b_on, b_c = summ(base, be, "CSS_package 값")
l_on, l_c = summ(lit, le, "문헌 표준값")
print(f"  → 취침 {abs(b_on-l_on):.2f}h, CBTmin {abs(b_c-l_c):.2f}h 차이")

hard = pcr.simulate(24*60, gate=0)
he = pcr.sleep_episodes(hard.t, hard.y[4])
summ(hard, he, "눈꺼풀 0% (하드게이팅)")
print(f"  → 눈꺼풀 3% 대비 취침 {abs(b_on - he[-1][0]%24):.2f}h 차이")

print("\n" + "="*46)
print("전체 결과:", "통과 ✅" if ok else "실패 ❌")
print("※ 원본 대조 회귀 게이트는 validate/compare.py 입니다.")
sys.exit(0 if ok else 1)
