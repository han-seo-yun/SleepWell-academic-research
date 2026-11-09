# -*- coding: utf-8 -*-
# =====================================================================
#  Sleep Stage 공통 평가 코드
#  [1단계] 형식/라벨 검증  →  [2단계] 정답·예측 매칭 후 지표 계산
#           (F1-score / Cohen's Kappa / Confusion Matrix)
# =====================================================================
#  ▶▶ 사용법: 아래 "여기만 수정" 두 줄의 경로만 본인 것으로 바꾸고 실행 ◀◀
#
#  필요 라이브러리:  pip install pandas scikit-learn numpy
#  예측 CSV 형식  :  subject_id, epoch_idx, stage_label  (night 있으면 매칭에 사용)
#                    stage_label 은 정수 0~4  (0=W,1=N1,2=N2,3=N3,4=REM)
# =====================================================================

# ┌───────────────── 여기만 수정 ─────────────────┐
GT_FOLDER   = r"C:\Users\본인\Desktop\Sleep-EDF_GroundTruth"   # 정답(Ground Truth) CSV 폴더 
PRED_FOLDER = r"C:\Users\본인\Desktop\예측폴더"                 # 예측 CSV 폴더 
TITLE       = "예측 vs 정답"                                   
# └───────────────────────────────────────────────┘

import os, glob
import numpy as np
import pandas as pd
from sklearn.metrics import f1_score, cohen_kappa_score, confusion_matrix, accuracy_score

# 수면 5단계를 정수로 표기 (이 5개 밖의 값은 평가에서 제외)
VALID_LABELS = [0, 1, 2, 3, 4]                       # 0=W 1=N1 2=N2 3=N3 4=REM
LABEL_NAMES  = ["W", "N1", "N2", "N3", "REM"]        # 출력용 이름

# 정답·예측을 짝지을 때 쓰는 "주소" (night는 선택)
KEY_CANDIDATES = ["subject_id", "night", "epoch_idx"]
# 예측 파일에 최소한 있어야 하는 열 (없으면 검증에서 걸림)
REQUIRED_MIN   = ["subject_id", "epoch_idx", "stage_label"]


def load_folder(path):
    """폴더 안의 *.csv 를 모두 읽어 하나로 합침.
       recursive=True 라서 부모 폴더만 지정해도 하위 SC/ST 안의 파일까지 다 읽음."""
    files = sorted(glob.glob(os.path.join(path, "**", "*.csv"), recursive=True))
    if not files:
        raise FileNotFoundError(f"CSV를 찾지 못했습니다: {path}")
    return pd.concat([pd.read_csv(f) for f in files], ignore_index=True)


def validate(df, name):
    """[1단계] 형식·라벨 검증.
       문제가 있으면 '무엇이 틀렸는지' 알리고 False 를 반환해 채점을 멈춤.
       ※ 값을 임의로 고치지 않음 — 낸 사람이 직접 고치도록 '거부'하는 방식."""
    ok = True
    print(f"\n[검증] {name}")

    # (1) 필수 열이 다 있는지
    missing = [c for c in REQUIRED_MIN if c not in df.columns]
    if missing:
        print(f"  ✗ 필수 열 없음: {missing}  (있어야 함: {REQUIRED_MIN})"); ok = False
    else:
        print(f"  ✓ 필수 열 있음: {REQUIRED_MIN}")

    if "stage_label" in df.columns:
        vals = pd.to_numeric(df["stage_label"], errors="coerce")   # 숫자로 못 바꾸면 NaN
        # (2) 라벨이 숫자인지
        n_nonnum = vals.isna().sum()
        if n_nonnum > 0:
            print(f"  ✗ 숫자가 아닌 stage_label {n_nonnum}개 (0~4 정수여야 함)"); ok = False
        # (3) 0~4 밖의 값 (movement/unknown 등) → 평가에서 빠짐 (오류는 아님, 안내만)
        outside = (~vals.isin(VALID_LABELS) & vals.notna()).sum()
        if outside > 0:
            print(f"  ⚠ 0~4 밖 라벨 {outside}개 → 평가에서 제외됨 (M/unknown 등)")
        else:
            print(f"  ✓ 모든 라벨이 0~4 범위")

    print(f"  · 총 {len(df)} 행")
    return ok


def matching_keys(gt, pred):
    """정답·예측을 짝지을 공통 열(주소)을 결정.
       subject_id/epoch_idx 는 필수, night 는 양쪽에 다 있을 때만 사용.
       → night 있으면 (사람, 밤, epoch) 3개로, 없으면 (사람, epoch) 2개로 매칭."""
    keys = [c for c in KEY_CANDIDATES if c in gt.columns and c in pred.columns]
    if "subject_id" not in keys or "epoch_idx" not in keys:
        raise ValueError(f"매칭 키 부족: subject_id/epoch_idx 필요. 현재 공통 키 {keys}")
    return keys


def evaluate(gt, pred, title="평가 결과"):
    """[2단계] 정답·예측을 주소로 맞춰 지표 계산."""
    keys = matching_keys(gt, pred)
    print(f"\n[매칭] 키 = {keys}")

    # 정답의 stage_label → y_true, 예측의 stage_label → y_pred 로 이름 바꿔서
    # 같은 주소(keys)끼리 inner join = "같은 사람·같은 시점"만 남김
    g = gt[keys + ["stage_label"]].rename(columns={"stage_label": "y_true"})
    p = pred[keys + ["stage_label"]].rename(columns={"stage_label": "y_pred"})
    merged = pd.merge(g, p, on=keys, how="inner")
    print(f"  · 정답 {len(g)} / 예측 {len(p)} → 매칭된 epoch {len(merged)}개")
    if len(merged) == 0:
        print("  ✗ 매칭된 게 없습니다. subject_id/night/epoch_idx가 서로 맞는지 확인하세요.")
        return None

    # 정답·예측 중 하나라도 0~4 밖(M/unknown 등)이면 그 epoch은 채점에서 제외
    before = len(merged)
    merged = merged[merged["y_true"].isin(VALID_LABELS) & merged["y_pred"].isin(VALID_LABELS)]
    if before - len(merged):
        print(f"  · 0~4 밖 값 포함 {before-len(merged)}개 제외 → 채점 대상 {len(merged)}개")

    y_true = merged["y_true"].astype(int).values
    y_pred = merged["y_pred"].astype(int).values

    # ── 지표 계산 ──
    acc  = accuracy_score(y_true, y_pred)                                              # 전체 정확도
    f1m  = f1_score(y_true, y_pred, labels=VALID_LABELS, average="macro",    zero_division=0)  # 단계 평등
    f1w  = f1_score(y_true, y_pred, labels=VALID_LABELS, average="weighted", zero_division=0)  # 개수 가중
    f1e  = f1_score(y_true, y_pred, labels=VALID_LABELS, average=None,       zero_division=0)  # 단계별
    kap  = cohen_kappa_score(y_true, y_pred, labels=VALID_LABELS)                      # 우연 보정 일치도
    cm   = confusion_matrix(y_true, y_pred, labels=VALID_LABELS)                       # 혼동 행렬

    # ── 출력 ──
    print(f"\n===== {title} =====")
    print(f"채점 대상 epoch : {len(y_true)}")
    print(f"Accuracy        : {acc:.4f}")
    # macro F1: 각 단계를 똑같은 비중으로 평균 → W처럼 많은 단계에 안 휘둘려 공정
    print(f"F1 (macro)      : {f1m:.4f}   ← 단계별 평등 평가(불균형에 공정)")
    print(f"F1 (weighted)   : {f1w:.4f}")
    # Cohen's Kappa: '아무렇게나 찍어도 맞을 정도'를 빼고 본 일치도 (0=찍기 수준, 1=완벽)
    print(f"Cohen's Kappa   : {kap:.4f}")
    print("\n단계별 F1:")
    for n, v in zip(LABEL_NAMES, f1e):
        print(f"   {n:<4}{v:.4f}")
    # Confusion Matrix: 행=실제 단계, 열=예측 단계. 대각선이 맞힌 것, 나머지가 틀린 것.
    print("\nConfusion Matrix (행=실제, 열=예측):")
    print("      " + "".join(f"{n:>7}" for n in LABEL_NAMES))
    for i, n in enumerate(LABEL_NAMES):
        print(f"  {n:<4}" + "".join(f"{cm[i][j]:>7}" for j in range(5)))
    return {"accuracy": acc, "f1_macro": f1m, "kappa": kap}


def run(gt_folder, pred_folder, title="평가 결과"):
    """전체 실행: 불러오기 → 검증(둘 다 통과해야) → 평가."""
    gt   = load_folder(gt_folder)
    pred = load_folder(pred_folder)
    if validate(gt, "Ground Truth") and validate(pred, "Prediction"):
        return evaluate(gt, pred, title)
    print("\n✗ 검증 실패 — 위 문제를 고친 뒤 다시 실행하세요. (채점 진행 안 함)")
    return None


if __name__ == "__main__":
    run(GT_FOLDER, PRED_FOLDER, TITLE)
