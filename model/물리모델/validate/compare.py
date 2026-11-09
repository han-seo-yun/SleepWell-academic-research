"""
MATLAB 원본(GNU Octave 실행) vs Python 포팅 수치 대조.

사용법:
    cd src/physics/validate && python compare.py

전제: 상위 폴더에 circadian.py / pcr.py, 저장소 CSS/ 와 ICbySleepHist/ 가 clone 되어 있을 것.
      Octave 기본 배포에 heaviside 가 없어 이 폴더의 heaviside.m 을 경로에 추가한다.

────────────────────────────────────────────────────────────────────────
★ 이 스크립트가 팀의 회귀 게이트다. test_physics.py 는 스모크 테스트일 뿐이다.
  뮤테이션 테스트 결과 test_physics.py 는 아래를 전부 통과시켰다:
    - dVdt(4) 의 1/2 계수 제거      (수면 8.13h -> 7.77h)
    - k = 0.55 누락
    - 눈꺼풀 게이트 통째로 제거
    - p 0.6 -> 0.5
  반면 이 스크립트는 위 넷을 각각 1.4e+00 / 3.9e-01 / 9.0e-03 / 4.9e-02 로 검출했다.

★ 안전장치 (이전 버전에 없어서 '틀렸는데 통과'가 가능했던 것들)
  1. Octave 종료코드 / stderr 확인 — 적분 실패를 조용히 넘기지 않는다
  2. 파싱된 행 수를 기대치와 대조 — 형식이 바뀌어 행이 누락되면 실패
  3. NaN 검사 — max(0.0, nan) == 0.0 이라 발산이 '오차 0'으로 집계되던 문제
  4. 공개 API(simulate) 를 직접 호출 — rhs 만 대조하면 simulate 의 기본
     tau_c 를 24.2 -> 25.5 로 바꿔도 통과했다
────────────────────────────────────────────────────────────────────────
"""
import os, re, subprocess, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import circadian as C
import pcr

TARGET = 1e-4


def _octave(script, expect_rows):
    out = subprocess.run(['octave', '--no-gui', '-q', script],
                         capture_output=True, text=True, cwd=HERE)
    if out.returncode != 0:
        raise RuntimeError(f"{script} 실행 실패 (rc={out.returncode})\n{out.stderr}")
    if re.search(r'^\s*error:', out.stderr, re.M):
        raise RuntimeError(f"{script} 에서 Octave 오류:\n{out.stderr}")

    rows = []
    for ln in out.stdout.splitlines():
        f = ln.split()
        if f and re.fullmatch(r'-?\d+(\.\d+)?', f[0]) and len(f) >= 4:
            try:
                rows.append([float(v) for v in f])
            except ValueError:
                raise RuntimeError(f"{script} 출력 파싱 실패: {ln!r}")
    if len(rows) != expect_rows:
        raise RuntimeError(
            f"{script}: 대조 행 {expect_rows}개를 기대했는데 {len(rows)}개를 얻었습니다.\n"
            f"--- stdout ---\n{out.stdout}")
    arr = np.array(rows, float)
    if not np.isfinite(arr).all():
        raise RuntimeError(f"{script}: 출력에 NaN/Inf 가 있습니다 (원본 적분 발산).\n{out.stdout}")
    return arr


def _table(ref, t_py, y_py, names, title):
    worst = 0.0
    print(f"── {title} ──")
    print(f"{'t(h)':>7} " + " ".join(f"{'|Δ'+n+'|':>11}" for n in names))
    for r in ref:
        k = int(np.argmin(abs(t_py - r[0])))
        d = np.array([abs(y_py[j][k] - r[j + 1]) for j in range(len(names))])
        if not np.isfinite(d).all():
            raise RuntimeError(f"Python 결과에 NaN 이 있습니다 (t={r[0]})")
        worst = max(worst, float(d.max()))
        print(f"{r[0]:7.2f} " + " ".join(f"{v:11.2e}" for v in d))
    print(f"→ 최대 절대오차 {worst:.3e}\n")
    return worst


def check_simpler():
    """simpler.m — 일주기 3변수. 공개 API circadian.simulate() 를 그대로 쓴다."""
    ref = _octave('run_simpler.m', expect_rows=5)
    T_END = 24 * 10
    # run_simpler.m 과 동일한 스케줄: 00:00~08:00 수면, 08:00~24:00 각성 250 lux
    light = C.sleep_wake_light([d * 24 for d in range(11)],
                               [d * 24 + 8 for d in range(11)], T_END, dt=2/60)
    t, x, y, n = C.simulate(light=light, t_span=(0, T_END),
                            V0=(-0.5, -0.25, 0.5), tau_c=24.2, dt=2/60)
    return _table(ref, t, [x, y, n], ['x', 'y', 'n'], 'simpler.m (일주기 3변수)')


def check_pcr():
    """PCR.m — 6변수. 공개 API pcr.simulate() 를 그대로 쓴다."""
    ref = _octave('run_pcr.m', expect_rows=5)
    T_END = 24 * 15
    sol = pcr.simulate(T_END, light_fn=pcr.baseline_light(T_END, dt=2/60), dt=2/60)
    return _table(ref, sol.t, sol.y, ['xc', 'x', 'n', 'Vv', 'Vm', 'H'],
                  'PCR.m (6변수 전체)')


if __name__ == '__main__':
    w = max(check_simpler(), check_pcr())
    print("=" * 52)
    ok = w < TARGET
    print(f"전체 최대 절대오차 = {w:.3e}   목표 {TARGET:.0e}  →  {'통과 ✅' if ok else '실패 ❌'}")
    sys.exit(0 if ok else 1)
