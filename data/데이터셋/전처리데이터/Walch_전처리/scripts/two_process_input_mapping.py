"""
Two-Process Model (Borbely) 입력값 매핑
- 입력: output/30s/{id}_unified_30s.csv, output/sleep_history_7d/{id}_sleep_history_7d.csv
- 출력:
    output/two_process_input/{id}_history_sleepwake_30s.csv  (Process S 시뮬레이션용 원본 시계열)
    output/two_process_input/{id}_two_process_params.csv     (subject별 S0, 서카디안 위상 앵커)
    output/two_process_input/model_constants.json           (팀 공용 시간상수, 문헌값)

주의(설계상 한계, 문서화 필요):
1. Walch 공개 데이터는 절대시각이 없어 진짜 circadian phase(예: DLMO, 심부체온 최저점)를
   알 수 없음. 대체값으로 "복원된 7일 수면 중앙시각의 평균(원형평균)"을 위상 앵커로 사용함.
   circadian_R(0~1)이 낮으면(=날마다 수면시각이 불규칙) 이 근사가 신뢰도 낮다는 뜻.
2. sleep_history_7d에서 복원되지 않은 날(day_has_recovered_sleep=False)은 전부 "각성"으로
   채움(보수적 기본값) -> S0가 실제보다 약간 높게(더 각성 압력 쌓인 것처럼) 나올 수 있음.
3. tau_i / tau_d는 문헌 표준값(Borbely/Achermann 1999, McCauley 2013 Three-Process Model 계열)이며
   Walch 데이터로부터 재추정한 값이 아님. 공식 구현 담당자가 필요시 조정.
"""

import json
import numpy as np
import pandas as pd
from pathlib import Path

BASE = Path(r"C:\Users\JSD\Desktop\Walch_처리\output")
IN_30S = BASE / "30s"
IN_HIST = BASE / "sleep_history_7d"
OUT_DIR = BASE / "two_process_input"

EPOCH_SEC = 30
WINDOW_DAYS = 7
DAY_SEC = 86400

# --- 문헌 표준 파라미터 (Borbely & Achermann 1999 / McCauley et al. 2013 계열) ---
TAU_I_HR = 18.2   # Process S 각성시 상승 시간상수 (h)
TAU_D_HR = 4.2    # Process S 수면시 하강 시간상수 (h)
S_UPPER = 1.0     # 상한 asymptote
S_LOWER = 0.0     # 하한 asymptote
S0_INIT_ASSUMPTION = 0.5  # 7일 시뮬레이션 시작점(가장 오래된 시점)의 가정 초기값
CIRCADIAN_PERIOD_HR = 24.0
CIRCADIAN_AMPLITUDE = None  # 정규화 단위에 따라 달라짐 -> 공식 구현 담당자가 설정


def build_history_sleepwake_30s(sid):
    hist = pd.read_csv(IN_HIST / f"{sid}_sleep_history_7d.csv")

    lo = -WINDOW_DAYS * DAY_SEC
    idx = pd.RangeIndex(lo // EPOCH_SEC, 0, name="epoch_idx")
    df = pd.DataFrame(index=idx)
    df["t_rel_sec"] = df.index * EPOCH_SEC
    df["is_sleep_hist"] = False

    recovered_days = set()
    for _, row in hist.iterrows():
        onset, wake = int(row["sleep_onset_t_rel_sec"]), int(row["wake_time_t_rel_sec"])
        mask = (df["t_rel_sec"] >= onset) & (df["t_rel_sec"] < wake)
        df.loc[mask, "is_sleep_hist"] = True
        recovered_days.add(int(row["day_index"]))

    day_index = (df["t_rel_sec"] // DAY_SEC * -1).astype(int)
    df["day_index"] = day_index
    df["day_has_recovered_sleep"] = df["day_index"].isin(recovered_days)
    df.insert(0, "subject_id", sid)
    return df.reset_index(), hist


def simulate_S_forward(history_df):
    is_sleep = history_df["is_sleep_hist"].to_numpy()
    dt_hr = EPOCH_SEC / 3600.0
    decay = np.exp(-dt_hr / TAU_D_HR)
    rise = np.exp(-dt_hr / TAU_I_HR)

    S = S0_INIT_ASSUMPTION
    for sleeping in is_sleep:
        if sleeping:
            S = S_LOWER + (S - S_LOWER) * decay
        else:
            S = S_UPPER - (S_UPPER - S) * rise
    return S


def circadian_phase_anchor(hist):
    if hist.empty:
        return {"circadian_phase_ref_sec": np.nan, "circadian_R": np.nan, "n_nights_used": 0}

    midpoints = (hist["sleep_onset_t_rel_sec"] + hist["wake_time_t_rel_sec"]) / 2.0
    phase_sec = np.mod(midpoints, DAY_SEC)
    theta = 2 * np.pi * phase_sec / DAY_SEC
    mean_sin, mean_cos = np.sin(theta).mean(), np.cos(theta).mean()
    R = float(np.hypot(mean_sin, mean_cos))
    mean_angle = np.mod(np.arctan2(mean_sin, mean_cos), 2 * np.pi)
    phase_ref_sec = float(mean_angle / (2 * np.pi) * DAY_SEC)
    return {
        "circadian_phase_ref_sec": round(phase_ref_sec, 1),
        "circadian_R": round(R, 3),
        "n_nights_used": len(hist),
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    subjects = sorted(p.stem.split("_")[0] for p in IN_HIST.glob("*_sleep_history_7d.csv"))
    param_rows = []
    for sid in subjects:
        history_df, hist = build_history_sleepwake_30s(sid)
        history_df.to_csv(OUT_DIR / f"{sid}_history_sleepwake_30s.csv", index=False)

        S0 = simulate_S_forward(history_df)
        phase = circadian_phase_anchor(hist)

        param_rows.append({
            "subject_id": sid,
            "S0_at_t0": round(S0, 4),
            **phase,
            "days_with_data_gap": WINDOW_DAYS - phase["n_nights_used"],
        })
        print(f"[{sid}] S0={S0:.4f}  phase_ref_sec={phase['circadian_phase_ref_sec']}  "
              f"R={phase['circadian_R']}  nights_used={phase['n_nights_used']}")

    params_df = pd.DataFrame(param_rows)
    params_df.to_csv(OUT_DIR / "two_process_params.csv", index=False)

    constants = {
        "tau_i_hr_wake_rise": TAU_I_HR,
        "tau_d_hr_sleep_decay": TAU_D_HR,
        "S_upper_asymptote": S_UPPER,
        "S_lower_asymptote": S_LOWER,
        "S0_init_assumption_at_window_start": S0_INIT_ASSUMPTION,
        "circadian_period_hr": CIRCADIAN_PERIOD_HR,
        "circadian_amplitude": CIRCADIAN_AMPLITUDE,
        "source": "Borbely & Achermann 1999; McCauley et al. 2013 Three-Process Model 계열 문헌값. "
                   "Walch 데이터로 재추정한 값 아님.",
    }
    with open(OUT_DIR / "model_constants.json", "w", encoding="utf-8") as f:
        json.dump(constants, f, ensure_ascii=False, indent=2)

    print("\n=== two_process_params.csv ===")
    print(params_df.to_string(index=False))
    print(f"\nlow-confidence subjects (circadian_R < 0.5):")
    print(params_df[params_df["circadian_R"] < 0.5][["subject_id", "circadian_R", "n_nights_used"]]
          .to_string(index=False))


if __name__ == "__main__":
    main()
