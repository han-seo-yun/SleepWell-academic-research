# -*- coding: utf-8 -*-
"""
Walch 전처리 결과를 Two-Process Model 입력으로 사용해
Process S / circadian C / sleep propensity의 방향성을 검증하는 참조 구현.

중요:
- C 정의는 기존 코드와 동일하게 유지한다.
  C(t) = A * cos(2π(t - phase_ref)/24), phase_ref에서는 C가 +최대.
  현재 phase_ref가 평균 midsleep이므로 C는 'sleep-promoting circadian drive'로 해석한다.
- 따라서 sleep propensity score는 S - C가 아니라 S + C로 정의한다.
- S(t)는 현재 epoch의 라벨을 적용하기 *전* 값을 score에 사용한다.
  따라서 같은 epoch 라벨이 score에 직접 들어가는 누수는 없다.
- 다만 S의 이전 상태 업데이트에는 실제 PSG sleep/wake 이력이 사용되므로,
  전체 epoch AUC는 독립적인 예측 성능이 아니라 '1-step history-conditioned diagnostic'이다.
- 부호/방향성 검증의 주 지표는 sleep onset score와 wakeup score를 직접 비교하는
  transition AUC 및 transition margin으로 둔다.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(r"C:\Users\JSD\Desktop\Walch_처리\output")

with open(OUT / "two_process_input" / "model_constants.json", encoding="utf-8") as f:
    C = json.load(f)

TAU_I = C["tau_i_hr_wake_rise"]
TAU_D = C["tau_d_hr_sleep_decay"]
S_UP, S_LO = C["S_upper_asymptote"], C["S_lower_asymptote"]
DEMO_CIRCADIAN_AMPLITUDE = 0.15  # 기존 C 정의 유지: 데모용 임시 진폭
EPOCH_SEC = 30


def simulate_two_process_for_night(row_params, night_df):
    """관측된 PSG 이력을 이용해 epoch 시작 시점의 S(t), C(t)를 계산한다.

    S_t[k]는 k번째 epoch의 is_sleep 라벨을 적용하기 전 값이다.
    즉 score[k] 계산에는 현재 epoch의 정답이 직접 사용되지 않는다.
    단, k 이전 epoch들의 실제 sleep/wake 상태는 S 업데이트에 사용된다.
    """
    S = float(row_params["S0_at_t0"])
    phase_ref_hr = float(row_params["circadian_phase_ref_sec"]) / 3600.0
    dt_hr = EPOCH_SEC / 3600.0
    decay = np.exp(-dt_hr / TAU_D)
    rise = np.exp(-dt_hr / TAU_I)

    S_series, C_series = [], []

    for t_sec, sleeping in zip(night_df["t_rel_sec"], night_df["is_sleep"]):
        t_hr = float(t_sec) / 3600.0

        # C 정의는 기존 코드 그대로 유지.
        # phase_ref(평균 midsleep)에서 +최대이므로 sleep-promoting drive로 해석.
        c_val = DEMO_CIRCADIAN_AMPLITUDE * np.cos(
            2 * np.pi * (t_hr - phase_ref_hr) / 24.0
        )

        # 현재 epoch 라벨을 적용하기 전에 기록 -> same-epoch leakage 방지
        S_series.append(S)
        C_series.append(c_val)

        if bool(sleeping):
            S = S_LO + (S - S_LO) * decay
        else:
            S = S_UP - (S_UP - S) * rise

    return np.asarray(S_series), np.asarray(C_series)


def auc(scores, labels):
    """binary AUC. label=1의 score가 더 클수록 좋은 방향."""
    labels = np.asarray(labels, dtype=int)
    scores = np.asarray(scores, dtype=float)

    valid = np.isfinite(scores) & np.isfinite(labels)
    labels, scores = labels[valid], scores[valid]

    pos, neg = scores[labels == 1], scores[labels == 0]
    if len(pos) == 0 or len(neg) == 0:
        return np.nan

    diff = pos[:, None] - neg[None, :]
    return float((diff > 0).mean() + 0.5 * (diff == 0).mean())


def transition_metrics(score, is_sleep):
    """sleep onset(0→1)과 wakeup(1→0)의 score 방향성을 검증한다.

    올바른 sleep-propensity score라면 일반적으로
      onset score > wakeup score
    방향을 기대한다.

    transition_auc는 onset=1, wakeup=0으로 두고 계산한다.
    0.5보다 크면 score 방향이 기대와 일치하는 경향,
    0.5보다 작으면 부호/위상/정렬 문제를 의심한다.
    """
    score = np.asarray(score, dtype=float)
    y = np.asarray(is_sleep, dtype=int)

    if len(y) < 2:
        return {
            "n_onset": 0,
            "n_wakeup": 0,
            "mean_onset_score": np.nan,
            "mean_wakeup_score": np.nan,
            "transition_margin": np.nan,
            "transition_auc_onset_vs_wakeup": np.nan,
        }

    prev = y[:-1]
    curr = y[1:]
    score_now = score[1:]

    onset_scores = score_now[(prev == 0) & (curr == 1)]
    wake_scores = score_now[(prev == 1) & (curr == 0)]

    mean_onset = float(np.mean(onset_scores)) if len(onset_scores) else np.nan
    mean_wakeup = float(np.mean(wake_scores)) if len(wake_scores) else np.nan

    if len(onset_scores) and len(wake_scores):
        trans_scores = np.concatenate([onset_scores, wake_scores])
        trans_labels = np.concatenate([
            np.ones(len(onset_scores), dtype=int),
            np.zeros(len(wake_scores), dtype=int),
        ])
        trans_auc = auc(trans_scores, trans_labels)
        margin = mean_onset - mean_wakeup
    else:
        trans_auc = np.nan
        margin = np.nan

    return {
        "n_onset": int(len(onset_scores)),
        "n_wakeup": int(len(wake_scores)),
        "mean_onset_score": mean_onset,
        "mean_wakeup_score": mean_wakeup,
        "transition_margin": margin,
        "transition_auc_onset_vs_wakeup": trans_auc,
    }


def state_direction_sanity(S_t, is_sleep):
    """S의 상태별 변화 방향 sanity check.

    S_t[k+1]-S_t[k]는 k번째 epoch 상태에 의해 결정되므로
    sleep이면 음수, wake이면 양수인지 비율을 본다.
    """
    S_t = np.asarray(S_t, dtype=float)
    y = np.asarray(is_sleep, dtype=bool)

    if len(S_t) < 2:
        return np.nan, np.nan

    dS = np.diff(S_t)
    state = y[:-1]

    sleep_delta = dS[state]
    wake_delta = dS[~state]

    sleep_ok = float(np.mean(sleep_delta < 0)) if len(sleep_delta) else np.nan
    wake_ok = float(np.mean(wake_delta > 0)) if len(wake_delta) else np.nan
    return sleep_ok, wake_ok


def main():
    params = pd.read_csv(
        OUT / "two_process_input" / "two_process_params.csv"
    ).set_index("subject_id")

    results = []

    for sid in params.index.tolist():
        unified = pd.read_csv(OUT / "30s" / f"{sid}_unified_30s.csv")

        # 실제 PSG로 채점 가능한 epoch만 사용
        night = unified[
            unified["sleep_stage_raw"].notna()
            & (unified["sleep_stage_raw"] != -1)
        ].copy()
        night = night.dropna(subset=["is_sleep"]).sort_values("t_rel_sec")

        if len(night) < 20:
            results.append({
                "subject_id": sid,
                "n_epochs": len(night),
                "auc_sleep_propensity_vs_sleep_1step": np.nan,
                "transition_auc_onset_vs_wakeup": np.nan,
                "transition_margin": np.nan,
            })
            continue

        row_params = params.loc[sid]
        S_t, C_t = simulate_two_process_for_night(row_params, night)

        # 핵심 수정:
        # C가 midsleep에서 +최대인 sleep-promoting drive이므로 더한다.
        sleep_propensity = S_t + C_t

        # 보조 진단값:
        # 현재 epoch score에는 현재 epoch 정답이 직접 들어가지 않지만,
        # 이전 epoch들의 실제 상태로 S를 업데이트했으므로 '독립 예측 성능'으로 해석하지 않는다.
        one_step_auc = auc(sleep_propensity, night["is_sleep"].astype(int))

        # 주 검증값: onset에서 score가 높고 wakeup에서 낮은지 직접 확인
        tm = transition_metrics(sleep_propensity, night["is_sleep"].astype(int))

        sleep_dir_ok, wake_dir_ok = state_direction_sanity(
            S_t, night["is_sleep"].astype(bool)
        )

        results.append({
            "subject_id": sid,
            "n_epochs": len(night),
            "S0_used": round(float(row_params["S0_at_t0"]), 4),
            "auc_sleep_propensity_vs_sleep_1step": round(one_step_auc, 3),
            "n_onset": tm["n_onset"],
            "n_wakeup": tm["n_wakeup"],
            "mean_onset_score": round(tm["mean_onset_score"], 4)
                if np.isfinite(tm["mean_onset_score"]) else np.nan,
            "mean_wakeup_score": round(tm["mean_wakeup_score"], 4)
                if np.isfinite(tm["mean_wakeup_score"]) else np.nan,
            "transition_margin": round(tm["transition_margin"], 4)
                if np.isfinite(tm["transition_margin"]) else np.nan,
            "transition_auc_onset_vs_wakeup": round(
                tm["transition_auc_onset_vs_wakeup"], 3
            ) if np.isfinite(tm["transition_auc_onset_vs_wakeup"]) else np.nan,
            "sleep_S_decrease_fraction": round(sleep_dir_ok, 3)
                if np.isfinite(sleep_dir_ok) else np.nan,
            "wake_S_increase_fraction": round(wake_dir_ok, 3)
                if np.isfinite(wake_dir_ok) else np.nan,
        })

    df = pd.DataFrame(results)
    out_path = OUT / "two_process_input" / "demo_validation_results.csv"
    df.to_csv(out_path, index=False)

    print(df.to_string(index=False))

    valid_1step = df["auc_sleep_propensity_vs_sleep_1step"].dropna()
    valid_trans = df["transition_auc_onset_vs_wakeup"].dropna()
    valid_margin = df["transition_margin"].dropna()

    print("\n=== 검증 요약 ===")
    print(f"처리된 subject: {len(df)}")

    if len(valid_1step):
        print(
            f"1-step history-conditioned AUC: 평균 {valid_1step.mean():.3f} "
            "(보조 진단값; 독립 예측 성능으로 해석 금지)"
        )

    if len(valid_trans):
        print(
            f"Transition AUC(onset>wakeup): 평균 {valid_trans.mean():.3f}, "
            f">0.5 subject {(valid_trans > 0.5).sum()} / {len(valid_trans)}"
        )

    if len(valid_margin):
        print(
            f"Transition margin(onset-wakeup): 평균 {valid_margin.mean():.4f}, "
            f">0 subject {(valid_margin > 0).sum()} / {len(valid_margin)}"
        )

    print(
        "\n판정 기준: transition AUC > 0.5 및 margin > 0이면 "
        "현재 C convention과 score 부호가 기대 방향과 일치."
    )
    print(
        "이 검증은 score의 부호/생리학적 방향성 확인용이며, "
        "최종 예측 성능은 별도 autonomous threshold/hysteresis 모델로 평가해야 함."
    )


if __name__ == "__main__":
    main()
