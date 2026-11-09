"""
pcr_ext.py — 팀 pcr.py 를 수정하지 않고 '강제 각성(forced wake)' 입력만 덧붙인 확장.

교대근무·수면 분절(무호흡) 실험에 쓴다.
원본 pcr.pcr_rhs 를 그대로 호출한 뒤, VLPO 방정식(dV_v)에만 각성 구동 W(t) 를 뺀다.
  dV_v' = dV_v - W(t) / TAU_V
PCR 계열 문헌(Phillips & Robinson 2007; Skeldon 2017 등)에서 사회적 제약으로 인한
강제 각성을 '수면 구동을 낮추는 추가 항'으로 넣는 방식과 같은 발상이다.
W(t)=0 이면 원본 모델과 완전히 같다 (test 에서 확인).
"""
import numpy as np
from scipy.integrate import solve_ivp
import pcr


def rhs_forced(t, V, light_fn, wake_fn, p):
    d = pcr.pcr_rhs(t, V, light_fn, p['tau_c'], p['mu'], p['v_vh'],
                    p['coef_x'], p['coef_y'], p['const'], p['gate'])
    d[3] -= wake_fn(t) / pcr.TAU_V
    return d


def simulate_forced(t_end_h, light_fn, wake_fn, V0=None, dt=2/60,
                    rtol=1e-8, atol=1e-10, max_step=0.05, **kw):
    p = {**pcr.CSS_DEFAULTS, **kw}
    V0 = list(pcr.V_00) if V0 is None else V0
    te = np.arange(0, t_end_h + dt / 2, dt)
    sol = solve_ivp(rhs_forced, (0, t_end_h), V0, t_eval=te,
                    args=(light_fn, wake_fn, p), method='LSODA',
                    rtol=rtol, atol=atol, max_step=max_step)
    if not sol.success:
        raise RuntimeError(sol.message)
    return sol


def daily_cbtmin(t, x, day_from, day_to, offset_h=12.0):
    """각 '일'의 CBTmin 시각(시계 시각). 하루 경계를 정오(offset)로 잡아
    새벽 최저점이 날짜 경계에 걸리지 않게 한다. 반환: [(day, abs_time_h)]"""
    out = []
    for d in range(day_from, day_to):
        lo, hi = d * 24 + offset_h, (d + 1) * 24 + offset_h
        m = (t >= lo) & (t < hi)
        if m.sum() == 0:
            continue
        out.append((d, t[m][np.argmin(x[m])]))
    return out
