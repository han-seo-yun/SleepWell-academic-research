"""
CSS/PCR.m 의 Python 포팅 (6변수)

원본 : https://github.com/Mathbiomed/CSS  (Hong et al. 2021, iScience)
계보 : Phillips, Chen & Robinson 2010 (수면-각성 flip-flop)
       ⊕ Forger, Jewett & Kronauer 1999 simpler model (일주기)
       ⊕ Skeldon 계열 결합 정식화

────────────────────────────────────────────────────────────────────────
★ 변수 대응 (PCR.m 은 Skeldon 규약 — simpler.m 과 x/y 가 반대)
    V(1) = x_c : van der Pol 비선형 항이 붙은 변수
    V(2) = x   : 빛 구동 B 가 붙은 변수      ← ★ CBTmin = min(V(2)), 오프셋 0
    V(3) = n   : 광수용체 활성화
    V(4) = V_v : VLPO (수면 촉진)
    V(5) = V_m : 단가아민 (각성 촉진)
    V(6) = H   : 항상성 수면압력

  CBTmin 근거: FJK 1999 식 (10) CBTmin = x_min + phi_ref, simpler model 은
  phi_ref = 0.0. 저자 코드 IC_mean_midsleep_method.m L31 도 동일.
  실측상 min(V(1)) 은 20:04, min(V(2)) 는 03:08 로 약 7시간 어긋난다.

★ 문헌과 다른 값 (2026-09-02 선행연구 대조 결과 — 발표에서 반드시 먼저 밝힐 것)
    coef_x : 우리 -0.16  /  문헌 -0.47   (bioRxiv 2026: C = ½(1+0.80y-0.47x))
    v_vc   : 우리  3.37  /  문헌  2.9    (Skeldon 2014, bioRxiv 2026)
    mu     : 우리  4.2   /  문헌  4.4    (PCR 2010, Skeldon 2014)
    v_vh   : 우리  1.01  /  문헌  1.0
  coef_x 와 v_vc 는 짝으로 조정된 것으로 보인다. 유효 진폭은
  v_vc*|(coef_y, coef_x)| 기준 문헌 2.691 vs 우리 2.749 로 2% 차이뿐이고,
  바뀐 것은 C 의 위상 19.1°(≈1.3h) 다. Hong et al. 2021 이 밝힌
  "28개 파라미터 중 3개를 실내 조명 프로파일에 맞게 수정"과 정합한다.
  (Table S5 원문 미확인 — 어느 3개인지 단정하지 말 것)

★ 눈꺼풀 감쇠 0.03 도 Hong et al. 의 자체 수정이다.
  PCR 계열 문헌(Skeldon 2017, PCR 2010, Postnova 2016)은 전부 수면 중 빛 = 0
  하드 게이팅을 쓴다. 방향은 생리학적으로 지지된다 —
  Bierman, Figueiro & Rea 2011 (J Biomed Opt) 이 닫힌 눈꺼풀 투과율을
  약 1~2% 로 측정. 0.03 은 그 상단이다. 민감도 분석 대상.
────────────────────────────────────────────────────────────────────────
"""
import numpy as np
from scipy.integrate import solve_ivp

# --- PCR.m 내부 고정 파라미터 (소스 그대로) ---
Q_MAX, THETA, SIGMA = 100.0, 10.0, 3.0     # s^-1, mV, mV
V_VM, V_MV, V_VC    = 2.1, 1.8, 3.37       # mV·s, mV·s, mV   (V_VC 는 문헌 2.9)
A_M, A_V            = 1.3, -10.2           # mV, mV (= PCR2010 의 D_0)
TAU_M = TAU_V       = 10.0 / 3600.0        # h (=10초)
CHI                 = 45.0                 # h
Q_TH                = 1.0                  # s^-1
KAPPA, I0, P, GAMMA = 12.0 / np.pi, 9500.0, 0.6, 0.23
K, BETA, F, ALPHA_0, B_C = 0.55, 0.013, 0.99669, 0.16, 0.4
G_LIGHT, LAMBDA     = 19.9, 60.0

# --- CSS_package.m L9, L31-32 의 실제 호출값 ---
CSS_DEFAULTS = dict(
    tau_c=24.09,      # 아침형 23.85 / 저녁형 24.35 (원본 주석)
    mu=4.2,
    v_vh=1.01,
    coef_x=-0.16,
    coef_y=0.8,
    const=1.0,
    gate=1,
)
V_00 = [-0.5, -0.25, 5.0, 0.0, -11.0, 14.25]   # CSS_package.m L60

# 문헌 표준값 — 민감도 분석/발표 대응용
LITERATURE_PARAMS = dict(tau_c=24.2, mu=4.4, v_vh=1.0,
                         coef_x=-0.47, coef_y=0.8, const=1.0, gate=1)


def Q(V):
    """발화율 시그모이드. PCR.m: Q_max ./ (1+exp(-(V-theta)/sigma))"""
    return Q_MAX / (1.0 + np.exp(-(V - THETA) / SIGMA))


def _eyelid_gate(V_m, gate):
    """
    PCR.m 의 눈꺼풀 감쇠항.
      h = heaviside(Q(V_m) - Q_th)
      게이트 = h + (h-1)*gate*(-0.03)
    각성(h=1) -> 1, 수면(h=0) -> 0.03.
    MATLAB heaviside(0) = 0.5 를 np.heaviside(., 0.5) 로 재현한다.
    """
    h = np.heaviside(Q(V_m) - Q_TH, 0.5)
    return h + (h - 1.0) * gate * (-0.03)


def pcr_rhs(t, V, I, tau_c, mu, v_vh, coef_x, coef_y, const, gate):
    x_c, x, n, V_v, V_m, H = V
    lux = I(t) if callable(I) else I
    g     = _eyelid_gate(V_m, gate)
    # alpha = alpha_0 * 게이트^p * (I/i_0)^p   ← ^p 가 두 인자에 각각 걸린다
    alpha = ALPHA_0 * (g ** P) * (max(lux, 0.0) / I0) ** P
    B     = G_LIGHT * alpha * (1 - n) * (1 - B_C * x_c) * (1 - B_C * x)
    C     = V_VC * 0.5 * (const + coef_y * x + coef_x * x_c)   # ★ 0.5 배 주의

    dx_c = (1/KAPPA) * (GAMMA*(x_c - 4*x_c**3/3)
                        - x*((24/(F*tau_c))**2 + K*B))
    dx   = (1/KAPPA) * (x_c + B)
    dn   = LAMBDA * (alpha*(1-n) - BETA*n)          # alpha 만. B 아님
    dV_v = (1/TAU_V) * (-V_v - V_VM*Q(V_m) + A_V - C + v_vh*H)
    dV_m = (1/TAU_M) * (-V_m - V_MV*Q(V_v) + A_M)
    dH   = (1/CHI)   * (-H + mu*Q(V_m))
    return [dx_c, dx, dn, dV_v, dV_m, dH]


# ──────────────────────────────────────────────────────────────────────
# 조도 입력
# ──────────────────────────────────────────────────────────────────────
def make_interp_light(t_grid, lux):
    """
    PCR.m L15 `I = interp1(it, i, t)` 와 동일한 선형보간 조도 함수를 만든다.
    ⚠ 경성 계단 함수를 쓰면 원본과 전이 순간이 최대 5.76 만큼 벌어진다
      (2분 격자 위 램프 vs 순간 점프). 반드시 원본과 같은 보간을 써야 한다.
    """
    t_grid, lux = np.asarray(t_grid, float), np.asarray(lux, float)
    return lambda t: float(np.interp(t, t_grid, lux))


def baseline_light(t_end_h, dt=2/60, on=6.0, off=22.0 - 0.3, lux=250.0):
    """
    CSS_package.m L44-53 의 기준 조명: 06:00 ~ 21:42 250 lux, 그 외 0.
    원본과 동일하게 2분 격자 + 선형보간으로 만든다.
    """
    t_grid = np.arange(0, t_end_h + dt, dt)
    h = t_grid % 24
    return make_interp_light(t_grid, np.where((h >= on) & (h < off), lux, 0.0))


def simulate(t_end_h, light_fn=None, V0=None, dt=2/60,
             rtol=1e-8, atol=1e-10, max_step=0.05, **kw):
    """
    6변수 모델을 적분한다.
    light_fn 을 안 주면 CSS_package.m 기준 조명을 원본과 동일한 방식으로 만든다.
    허용오차 기본값은 validate/compare.py 의 원본 대조와 동일하게 맞춰 두었다
    (검증한 설정과 출하하는 설정이 달라지지 않게).
    """
    p = {**CSS_DEFAULTS, **kw}
    V0 = list(V_00) if V0 is None else V0
    if light_fn is None:
        light_fn = baseline_light(t_end_h, dt=dt)
    te = np.arange(0, t_end_h + dt / 2, dt)   # 끝점 포함(원본 it 격자와 동일)
    sol = solve_ivp(pcr_rhs, (0, t_end_h), V0, t_eval=te,
                    args=(light_fn, p['tau_c'], p['mu'], p['v_vh'],
                          p['coef_x'], p['coef_y'], p['const'], p['gate']),
                    method='LSODA', rtol=rtol, atol=atol, max_step=max_step)
    if not sol.success:
        raise RuntimeError(f"적분 실패: {sol.message}")
    return sol


def is_asleep(V_m):
    """각성 뉴런 발화율이 임계 이하이면 수면. PCR.m 의 Q_th = 1."""
    return Q(V_m) < Q_TH


def sleep_episodes(t, V_m):
    """(onset, offset) 쌍 목록. onset 뒤의 첫 offset 을 짝짓는다."""
    a = is_asleep(np.asarray(V_m))
    d = np.diff(a.astype(int))
    t = np.asarray(t)
    on, off = t[1:][d == 1], t[1:][d == -1]
    return [(o, off[off > o][0]) for o in on if (off > o).any()]
