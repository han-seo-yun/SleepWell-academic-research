"""
Walch(Apple Watch) 데이터셋 전처리
1) 30초 단위 공통 포맷 통합 (heart_rate + motion + steps + labels)
2) Subject별 최근 7일 sleep/wake 이력 복원 (steps 기반 actigraphy)

입력: C:\\Users\\JSD\\Desktop\\데이터 폴더\\{heart_rate,motion,steps,labels}
출력: C:\\Users\\JSD\\Desktop\\Walch_처리\\output\\30s\\{id}_unified_30s.csv
      C:\\Users\\JSD\\Desktop\\Walch_처리\\output\\sleep_history_7d\\{id}_sleep_history_7d.csv
      C:\\Users\\JSD\\Desktop\\Walch_처리\\output\\summary.csv
"""

import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR = Path(r"C:\Users\JSD\Desktop\데이터 폴더")
OUT_DIR = Path(r"C:\Users\JSD\Desktop\Walch_처리\output")

EPOCH_SEC = 30
MOTION_CHUNK_ROWS = 2_000_000

WINDOW_DAYS = 7
GAP_TOLERANCE_MIN = 10
MIN_SLEEP_BOUT_MIN = 90
REST_STEPS_5MIN_THRESHOLD = 0  # 5분 버킷 합계가 이 값 이하이면 "휴식"으로 간주
MIN_BOUT_COVERAGE = 0.5  # bout 내 실측 데이터 비율이 이보다 낮으면(착용 해제 구간) 폐기

STAGE_MAP = {-1: "Unscored", 0: "Wake", 1: "N1", 2: "N2", 3: "N3", 4: "N3", 5: "REM"}


def list_subjects():
    ids = sorted(
        p.stem.split("_")[0] for p in (DATA_DIR / "labels").glob("*_labeled_sleep.csv")
    )
    return ids


def load_labels(sid):
    df = pd.read_csv(DATA_DIR / "labels" / f"{sid}_labeled_sleep.csv")
    df["epoch_idx"] = (df["epoch"] // EPOCH_SEC).astype(np.int64)
    df["sleep_stage_raw"] = df["label"].astype(int)
    return df.set_index("epoch_idx")[["sleep_stage_raw"]]


def load_heart_rate(sid):
    df = pd.read_csv(DATA_DIR / "heart_rate" / f"{sid}_heartrate.csv")
    df["epoch_idx"] = np.floor_divide(df["timestamp"].to_numpy(), EPOCH_SEC).astype(np.int64)
    g = df.groupby("epoch_idx")["hr"].agg(hr_mean="mean", hr_std="std", hr_n="count")
    return g


def load_steps(sid):
    df = pd.read_csv(DATA_DIR / "steps" / f"{sid}_steps.txt", header=None, names=["t0", "count"])
    df = df.sort_values("t0").reset_index(drop=True)
    # 각 구간의 폭(다음 구간 시작까지)을 계산, 마지막 행은 직전 구간 폭을 재사용
    widths = df["t0"].diff().shift(-1)
    widths.iloc[-1] = widths.iloc[:-1].median() if len(widths) > 1 else 600
    widths = widths.fillna(600).clip(lower=EPOCH_SEC)
    df["width"] = widths

    rows_t0 = []
    rows_steps = []
    for t0, count, width in zip(df["t0"], df["count"], df["width"]):
        n_sub = max(int(round(width / EPOCH_SEC)), 1)
        per_sub = count / n_sub
        sub_t0 = t0 + EPOCH_SEC * np.arange(n_sub)
        rows_t0.append(sub_t0)
        rows_steps.append(np.full(n_sub, per_sub))
    t_arr = np.concatenate(rows_t0)
    s_arr = np.concatenate(rows_steps)
    epoch_idx = np.floor_divide(t_arr, EPOCH_SEC).astype(np.int64)
    out = pd.DataFrame({"epoch_idx": epoch_idx, "steps": s_arr})
    return out.groupby("epoch_idx")["steps"].sum()


def load_motion(sid):
    path = DATA_DIR / "motion" / f"{sid}_acceleration.csv"
    parts = []
    for chunk in pd.read_csv(
        path, chunksize=MOTION_CHUNK_ROWS,
        dtype={"timestamp": np.float64, "x": np.float32, "y": np.float32, "z": np.float32},
    ):
        mag = np.sqrt(chunk["x"].to_numpy(dtype=np.float64) ** 2
                       + chunk["y"].to_numpy(dtype=np.float64) ** 2
                       + chunk["z"].to_numpy(dtype=np.float64) ** 2)
        epoch_idx = np.floor_divide(chunk["timestamp"].to_numpy(), EPOCH_SEC).astype(np.int64)
        tmp = pd.DataFrame({"epoch_idx": epoch_idx, "mag": mag, "mag2": mag ** 2})
        g = tmp.groupby("epoch_idx").agg(sum=("mag", "sum"), sumsq=("mag2", "sum"), n=("mag", "count"))
        parts.append(g)

    combined = pd.concat(parts).groupby(level=0).sum()
    mean = combined["sum"] / combined["n"]
    var = np.clip(combined["sumsq"] / combined["n"] - mean ** 2, 0, None)
    std = np.sqrt(var)
    return pd.DataFrame(
        {"activity_mean": mean, "activity_std": std, "activity_n": combined["n"].astype(int)}
    )


def build_unified_30s(sid):
    labels = load_labels(sid)
    hr = load_heart_rate(sid)
    steps = load_steps(sid)
    motion = load_motion(sid)

    lo = min(labels.index.min(), hr.index.min(), steps.index.min(), motion.index.min())
    hi = max(labels.index.max(), hr.index.max(), steps.index.max(), motion.index.max())
    full_idx = pd.RangeIndex(lo, hi + 1, name="epoch_idx")

    out = pd.DataFrame(index=full_idx)
    out = out.join(hr).join(motion).join(steps.rename("steps")).join(labels)

    out["has_motion"] = out["activity_n"].notna()
    out["sleep_stage_raw"] = out["sleep_stage_raw"].astype("Int64")
    out["sleep_stage"] = out["sleep_stage_raw"].map(STAGE_MAP)
    out["is_sleep"] = out["sleep_stage"].map(
        lambda s: True if s in ("N1", "N2", "N3", "REM") else (False if s == "Wake" else np.nan)
    )

    out = out.reset_index()
    out.insert(0, "subject_id", sid)
    out["t_rel_sec"] = out["epoch_idx"] * EPOCH_SEC
    cols = [
        "subject_id", "epoch_idx", "t_rel_sec",
        "hr_mean", "hr_std", "hr_n",
        "activity_mean", "activity_std", "activity_n", "has_motion",
        "steps",
        "sleep_stage_raw", "sleep_stage", "is_sleep",
    ]
    return out[cols]


def reconstruct_sleep_history_7d(unified):
    sid = unified["subject_id"].iloc[0]
    df = unified[["t_rel_sec", "steps"]].copy()
    df = df[(df["t_rel_sec"] >= -WINDOW_DAYS * 86400) & (df["t_rel_sec"] < 0)]
    if df.empty:
        return pd.DataFrame(columns=[
            "subject_id", "day_index", "sleep_onset_t_rel_sec", "wake_time_t_rel_sec",
            "duration_sec", "duration_hr", "data_coverage_frac",
        ])

    df["bin5"] = (df["t_rel_sec"] // 300).astype(np.int64)
    bins = df.groupby("bin5").agg(steps_sum=("steps", "sum"), n=("steps", "count"))
    full_bins = pd.RangeIndex(
        int(-WINDOW_DAYS * 86400 // 300), 0, name="bin5"
    )
    bins = bins.reindex(full_bins)
    bins["has_data"] = bins["n"].notna() & (bins["n"] > 0)
    # 결측(has_data=False) 구간은 "휴식"이 아니라 "미지"로 취급 - 웨어러블 미착용 구간이
    # 장기 수면으로 오인되는 것을 방지 (실측 steps<=threshold일 때만 rest)
    bins["is_rest"] = bins["has_data"] & (bins["steps_sum"].fillna(0) <= REST_STEPS_5MIN_THRESHOLD)

    gap_tol_bins = max(int(round(GAP_TOLERANCE_MIN / 5)), 1)
    rest = bins["is_rest"].to_numpy().copy()
    # 짧은 활동(gap) 브릿징: gap_tol_bins 이하로 끼인 non-rest 구간을 rest로 간주
    i = 0
    n = len(rest)
    while i < n:
        if not rest[i]:
            j = i
            while j < n and not rest[j]:
                j += 1
            gap_len = j - i
            left_rest = i > 0 and rest[i - 1]
            right_rest = j < n and rest[j]
            if gap_len <= gap_tol_bins and left_rest and right_rest:
                rest[i:j] = True
            i = j
        else:
            i += 1
    bins["is_rest_bridged"] = rest

    bin_starts = bins.index.to_numpy() * 300
    min_bout_bins = max(int(round(MIN_SLEEP_BOUT_MIN / 5)), 1)

    bouts = []
    i = 0
    while i < n:
        if bins["is_rest_bridged"].iloc[i]:
            j = i
            while j < n and bins["is_rest_bridged"].iloc[j]:
                j += 1
            if (j - i) >= min_bout_bins:
                onset = bin_starts[i]
                wake = bin_starts[j - 1] + 300
                coverage = bins["has_data"].iloc[i:j].mean()
                if coverage >= MIN_BOUT_COVERAGE:
                    bouts.append((onset, wake, coverage))
            i = j
        else:
            i += 1

    records = []
    for day_index in range(WINDOW_DAYS, 0, -1):
        w_lo = -day_index * 86400
        w_hi = w_lo + 86400
        day_bouts = [b for b in bouts if b[0] >= w_lo and b[0] < w_hi]
        if not day_bouts:
            continue
        onset, wake, coverage = max(day_bouts, key=lambda b: b[1] - b[0])
        records.append({
            "subject_id": sid,
            "day_index": day_index,
            "sleep_onset_t_rel_sec": onset,
            "wake_time_t_rel_sec": wake,
            "duration_sec": wake - onset,
            "duration_hr": round((wake - onset) / 3600, 2),
            "data_coverage_frac": round(float(coverage), 3),
        })

    return pd.DataFrame(records).sort_values("sleep_onset_t_rel_sec")


def main():
    (OUT_DIR / "30s").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "sleep_history_7d").mkdir(parents=True, exist_ok=True)

    subjects = list_subjects()
    summary_rows = []
    for sid in subjects:
        print(f"[{sid}] processing...")
        unified = build_unified_30s(sid)
        unified.to_csv(OUT_DIR / "30s" / f"{sid}_unified_30s.csv", index=False)

        hist = reconstruct_sleep_history_7d(unified)
        hist.to_csv(OUT_DIR / "sleep_history_7d" / f"{sid}_sleep_history_7d.csv", index=False)

        summary_rows.append({
            "subject_id": sid,
            "n_epochs_30s": len(unified),
            "n_epochs_with_hr": unified["hr_mean"].notna().sum(),
            "n_epochs_with_motion": unified["has_motion"].sum(),
            "n_epochs_with_steps": unified["steps"].notna().sum(),
            "n_labeled_epochs": unified["sleep_stage_raw"].notna().sum(),
            "n_sleep_history_days_found": len(hist),
            "mean_history_duration_hr": round(hist["duration_hr"].mean(), 2) if len(hist) else np.nan,
        })
        print(f"[{sid}] done. unified={len(unified)} rows, history_days={len(hist)}")

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False)
    print("\n=== SUMMARY ===")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
