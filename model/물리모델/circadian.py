"""
ICbySleepHist/simpler.m 의 Python 포팅 (일주기 진동자 3변수)

원본 : https://github.com/Mathbiomed/ICbySleepHist  (Lim et al. 2025, J Biol Rhythms)
계보 : Forger, Jewett & Kronauer 1999, J Biol Rhythms
       "A Simpler Model of the Human Circadian Pacemaker"

────────────────────────────────────────────────────────────────────────
★ CBTmin 은 x(=V(1)) 의 최소점이다. y 가 아니다. 오프셋도 0 이다.

  근거 1 (원논문) FJK 1999 식 (10):
        CBTmin = x_min + phi_ref,  그리고 simpler model 적합 결과 phi_ref = 0.0
  근거 2 (저자 코드) ICbySleepHist/IC_mean_midsleep_method.m L31:
        [~, CBTmin] = min(one_period(:, 1));     % 1열 = x

  ⚠ JFK 1999 "revised(higher-order)" 모델은 CBTmin 을 위상각 -170.7° + 0.97h
    로 정의한다(Postnova 2016). 그건 다른 모델이므로 여기에 적용하면 안 된다.
    구분 기준: 이 파일은 3차 비선형 + mu=0.23 + f=0.99669 -> simpler model.

★ 표기 규약 주의
  simpler.m  : 비선형항이 y, 빛 구동 B 가 x        (FJK 1999 규약)
  CSS/PCR.m  : 비선형항이 V(1), 빛 구동 B 가 V(2)  (Skeldon 규약, x/y 반대)
  즉 simpler 의 x = PCR 의 V(2) 다. 두 파일을 섞어 쓸 때 반드시 확인할 것.

★ 기호 충돌 주의
  simpler.m 의 mu = 0.23 은 van der Pol 강성 계수로, PCR.m 의 gamma 와 같다.
  PCR.m 의 mu(=4.2) 는 수면압력 결합 계수로 전혀 다른 파라미터다.
────────────────────────────────────────────────────────────────────────
"""
import numpy as np
from scipy.integrate import solve_ivp

# --- simpler.m 파라미터 (소스 그대로 / 문헌 대조 완료) ---
KAPPA   = 12.0 / np.pi   # FJK1999
I0      = 9500.0         # lux
P       = 0.6
K       = 0.55
BETA    = 0.013
F       = 0.99669        # ★ simpler model 전용. revised 모델의 0.99729 와 다름
ALPHA_0 = 0.16
B_COEF  = 0.4
MU_VDP  = 0.23           # = PCR.m 의 gamma. PCR.m 의 mu(4.2) 와 혼동 금지
G       = 19.9
LAMBDA  = 60.0

LUX_WAKE  = 250.0        # 참고문헌의 가정 조도
LUX_SLEEP = 0.0


def circadian_rhs(t, V, I, tau_c=24.2):
    """t[h], V=[x, y, n], I = 상수 조도(lux) 또는 t 를 받는 함수"""
    x, y, n = V
    lux = I(t) if callable(I) else I
    alpha = ALPHA_0 * (max(lux, 0.0) / I0) ** P
    Bd    = G * alpha * (1 - n) * (1 - B_COEF * x) * (1 - B_COEF * y)
    dx = (1 / KAPPA) * (y + Bd)
    dy = (1 / KAPPA) * (MU_VDP * (y - 4 * y**3 / 3)
                        - x * ((24 / (F * tau_c))**2 + K * Bd))
    dn = LAMBDA * (alpha * (1 - n) - BETA * n)
    return [dx, dy, dn]


# ──────────────────────────────────────────────────────────────────────
# 수면/각성 스케줄 -> 조도 구간
# ──────────────────────────────────────────────────────────────────────
def sleep_wake_segments(onsets, offsets, t_end, t_start=0.0,
                        lux_wake=LUX_WAKE, lux_sleep=LUX_SLEEP):
    """
    수면 onset/offset 시각 목록을 [(t0, t1, lux), ...] 구간 리스트로 변환한다.

    ICbySleepHist/convert_sleep_vector.m 의 상태머신을 그대로 옮긴 것으로,
    원본이 명시적으로 처리하는 아래 세 경우를 모두 지원한다.

      (a) 기록 시작 시점에 이미 자고 있는 경우
          원본 L24: if min(onset_ind) > min(offset_ind) -> sleep_vector = ones(...)
          여기서는 첫 이벤트가 offset 이면 시작 상태를 '수면'으로 둔다.
      (b) onset/offset 개수 불일치 (마지막 수면이 안 끝났거나 그 반대)
          원본 L31: while ~isempty(onset_ind) || ~isempty(offset_ind)
      (c) 입력이 시각순으로 정렬돼 있지 않은 경우
          원본 L32-33: 매 반복마다 min() 을 취하므로 정렬이 불필요

    ⚠ 이전 구현은 zip(onsets, offsets) 로 순서대로 짝지었기 때문에 위 세 경우에서
      수면 구간이 경고 없이 사라지고 시간축이 역행했다. 기록 시작 시각에 이미
      자고 있는 피험자(액티그래피에서 가장 흔한 경우)는 CBTmin 이 통째로 무의미해졌다.
    """
    ev = sorted([(float(v), 1) for v in onsets] +       # 1 = 수면 시작
                [(float(v), 0) for v in offsets])       # 0 = 수면 종료
    t_start, t_end = float(t_start), float(t_end)
    if t_end <= t_start:
        raise ValueError(f"t_end({t_end}) 가 t_start({t_start}) 보다 커야 합니다.")

    # 첫 이벤트가 offset 이면 기록 시작 시점에 이미 자고 있었다는 뜻
    asleep = bool(ev) and ev[0][1] == 0

    segs, cur = [], t_start
    for t, kind in ev:
        if t <= cur:                 # 기록 시작 이전 이벤트: 상태만 갱신
            asleep = bool(kind)
            continue
        if t >= t_end:
            break
        segs.append((cur, t, lux_sleep if asleep else lux_wake))
        asleep = bool(kind)
        cur = t
    segs.append((cur, t_end, lux_sleep if asleep else lux_wake))

    segs = [s for s in segs if s[1] > s[0]]
    # 구간이 빈틈없이 이어지는지 자체 점검 (조용한 소실 방지)
    for a, b in zip(segs, segs[1:]):
        if abs(a[1] - b[0]) > 1e-9:
            raise RuntimeError(f"구간이 불연속입니다: {a} -> {b}")
    return segs


def make_interp_light(t_grid, lux):
    """simpler.m L3 `interp1(It, I, t)` 와 동일한 선형보간 조도 함수."""
    t_grid, lux = np.asarray(t_grid, float), np.asarray(lux, float)
    return lambda t: float(np.interp(t, t_grid, lux))


def segments_to_light(segments, dt=1/60):
    """
    구간 리스트를 원본과 같은 '격자 + 선형보간' 조도 함수로 바꾼다.

    ⚠ 원본 ICbySleepHist 는 수면/각성 벡터를 1분 격자의 조도 벡터로 만든 뒤
      interp1 로 넘긴다(IC_mean_midsleep_method.m L17-21). 즉 전이 순간이
      격자 한 칸에 걸쳐 램프된다. 구간별 상수로 나눠 적분하면 그 램프가 없어져
      원본과 최대 1.2e-02 까지 벌어진다(측정치). 원본 재현이 목적이므로
      이쪽을 기본으로 쓴다.
    """
    t0, t_end = segments[0][0], segments[-1][1]
    t_grid = np.arange(t0, t_end + dt, dt)
    lux = np.empty_like(t_grid)
    for a, b, l in segments:
        lux[(t_grid >= a) & (t_grid < b)] = l
    lux[t_grid >= segments[-1][1]] = segments[-1][2]
    return make_interp_light(t_grid, lux)


def sleep_wake_light(onsets, offsets, t_end, dt=1/60, t_start=0.0,
                     lux_wake=LUX_WAKE, lux_sleep=LUX_SLEEP):
    """
    onset/offset 이벤트로부터 원본과 동일한 방식의 조도 함수를 직접 만든다.

    원본 ICbySleepHist 는 수면/각성을 분 단위 벡터로 펼친 뒤 조도로 바꿔
    interp1 에 넘긴다. 이 함수는 그 과정을 그대로 재현하며,
    특히 **격자 마지막 점(t_end)에서의 상태 전환까지** 반영한다.
    (segments 경유 경로는 t_end 에 정확히 걸린 이벤트를 잃어버려
     마지막 dt 구간의 조도 램프가 달라지고 n 이 5.6e-03 어긋났다.)
    """
    t_grid = np.arange(float(t_start), float(t_end) + dt / 2, dt)
    ev = sorted([(float(v), 1) for v in onsets] + [(float(v), 0) for v in offsets])
    if not ev:
        return make_interp_light(t_grid, np.full_like(t_grid, lux_wake))
    et = np.array([e[0] for e in ev])
    es = np.array([e[1] for e in ev], dtype=int)       # 1=수면 시작
    start_asleep = int(ev[0][1] == 0)
    k = np.searchsorted(et, t_grid, side='right')      # t 이하의 이벤트 개수
    state = np.where(k == 0, start_asleep, es[np.clip(k - 1, 0, None)])
    return make_interp_light(t_grid, np.where(state == 1, lux_sleep, lux_wake))


def simulate(segments=None, V0=(-0.17, -1.22, 0.5), tau_c=24.2, dt=1/60,
             rtol=1e-8, atol=1e-10, max_step=0.05, light_mode='interp',
             light=None, t_span=None):
    """
    일주기 3변수 모델을 적분한다.

    light_mode='interp' (기본, 원본 재현)
        조도를 dt 격자 + 선형보간으로 만들어 한 번에 적분한다. simpler.m 의
        interp1 과 동일하다. 원본 대조 검증(validate/compare.py)은 이 모드로 한다.
    light_mode='segment'
        구간마다 상수 조도로 나눠 적분한다. 순수 계단 입력에 대한 수치적으로
        더 깨끗한 해지만 원본과는 다르다.

    ⚠ 구간 분할 모드에서 상태 인계는 반드시 t1 에서 해야 한다. t_eval 의
      마지막 점은 t1-dt 이므로 sol.y[:, -1] 을 쓰면 경계마다 궤적이 dt 만큼
      되감긴다. 그 버그가 있을 때 12일 뒤 CBTmin 이 출력 간격에 따라
      5.65h~6.00h 로 21분 움직였다. dense_output 으로 t1 상태를 받아 해결.
    """
    V0 = np.asarray(V0, dtype=float)
    if light is not None:
        if t_span is None:
            raise ValueError("light 를 직접 줄 때는 t_span=(t0, t1) 도 필요합니다.")
        t_start, t_end = t_span
        light_mode = 'interp'
    elif segments:
        t_start, t_end = segments[0][0], segments[-1][1]
    else:
        raise ValueError("적분할 구간이 없습니다.")

    if light_mode == 'interp':
        if light is None:
            light = segments_to_light(segments, dt=dt)
        te = np.arange(t_start, t_end + dt / 2, dt)
        sol = solve_ivp(circadian_rhs, (t_start, t_end), V0, args=(light, tau_c),
                        t_eval=te, method='LSODA',
                        rtol=rtol, atol=atol, max_step=max_step)
        if not sol.success:
            raise RuntimeError(f"적분 실패: {sol.message}")
        return sol.t, sol.y[0], sol.y[1], sol.y[2]

    if light_mode != 'segment':
        raise ValueError(f"light_mode 는 'interp' 또는 'segment' 여야 합니다: {light_mode!r}")

    ts, xs, ys, ns = [], [], [], []
    V = V0
    for t0, t1, lux in segments:
        te = np.arange(t0, t1, dt)
        sol = solve_ivp(circadian_rhs, (t0, t1), V, args=(lux, tau_c),
                        t_eval=te, dense_output=True,
                        method='LSODA', rtol=rtol, atol=atol, max_step=max_step)
        if not sol.success:
            raise RuntimeError(f"적분 실패 ({t0}~{t1}h): {sol.message}")
        if te.size:
            ts.append(sol.t); xs.append(sol.y[0]); ys.append(sol.y[1]); ns.append(sol.y[2])
        V = sol.sol(t1)          # ★ t1-dt 가 아니라 t1 에서 인계
    return (np.concatenate(ts), np.concatenate(xs),
            np.concatenate(ys), np.concatenate(ns))


# ──────────────────────────────────────────────────────────────────────
# 극소점 탐색
# ──────────────────────────────────────────────────────────────────────
def local_minima(t, v):
    """
    진짜 극소점(1차 차분의 부호 전환)만 반환한다.

    ⚠ 이전 구현은 24h 창마다 argmin 을 취했는데, 그건 극소점이 아니라 그냥
      '창 안의 최솟값 샘플'이다. 진짜 극소가 창 밖에 있으면 창의 끝점을 집어
      가짜 최소를 만든다. 자유진동에서 위상이 자정을 가로지르는 순간
      CBTmin 이 23:58 -> 00:04 로 24h 점프하는 현상이 여기서 나왔다.
    """
    t, v = np.asarray(t), np.asarray(v)
    if t.size < 3:
        return np.empty(0)
    d = np.diff(v)
    idx = np.nonzero((d[:-1] < 0) & (d[1:] >= 0))[0] + 1
    return t[idx]


def daily_minima(t, v, day0=None, day1=None, t_ref=0.0):
    """
    v 의 극소점을 찾아 24h 창(day) 단위로 걸러 반환한다.
    day 는 t_ref 기준 floor((t - t_ref)/24).
    """
    tm = local_minima(t, v)
    if tm.size == 0:
        return tm
    day = np.floor((tm - t_ref) / 24).astype(int)
    keep = np.ones(tm.size, dtype=bool)
    if day0 is not None:
        keep &= day >= day0
    if day1 is not None:
        keep &= day < day1
    return tm[keep]
