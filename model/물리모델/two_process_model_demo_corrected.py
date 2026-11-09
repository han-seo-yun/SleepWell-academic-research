#!/usr/bin/env python3
"""Corrected Two-Process (S+C_sleep) diagnostic for epoch-labelled sleep data.

Input CSV columns: subject_id, timestamp, is_sleep (0/1).
The homeostatic state S is updated from the observed previous epoch, so epoch
AUC is explicitly history-conditioned and is not an independent forecast AUC.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score


@dataclass(frozen=True)
class ModelConfig:
    tau_wake_h: float = 18.2
    tau_sleep_h: float = 4.2
    s_upper: float = 1.0
    s_lower: float = 0.0
    initial_s: float = 0.5
    circadian_amplitude: float = 0.15


def circular_mean_hours(hours: np.ndarray) -> float:
    """Return the 24-hour circular mean in [0, 24)."""
    angles = 2.0 * np.pi * np.asarray(hours, dtype=float) / 24.0
    z = np.mean(np.exp(1j * angles))
    if np.isclose(abs(z), 0.0):
        raise ValueError("Circadian phase is undefined: circular mean is zero.")
    return float((np.angle(z) * 24.0 / (2.0 * np.pi)) % 24.0)


def infer_midsleep_phase(frame: pd.DataFrame) -> float:
    """Infer phase_ref from contiguous observed sleep bouts' midpoints."""
    sleep = frame["is_sleep"].to_numpy(dtype=int)
    ts = frame["timestamp"]
    padded = np.r_[0, sleep, 0]
    starts = np.flatnonzero(np.diff(padded) == 1)
    ends = np.flatnonzero(np.diff(padded) == -1) - 1
    if len(starts) == 0:
        raise ValueError("At least one sleep bout is required to infer midsleep.")
    mids = [ts.iloc[a] + (ts.iloc[b] - ts.iloc[a]) / 2 for a, b in zip(starts, ends)]
    mids_h = np.array([m.hour + m.minute / 60 + m.second / 3600 for m in mids])
    return circular_mean_hours(mids_h)


def homeostatic_process(frame: pd.DataFrame, cfg: ModelConfig) -> np.ndarray:
    """Update S from the preceding observed epoch (history-conditioned)."""
    times = frame["timestamp"].astype("int64").to_numpy() / 3.6e12
    labels = frame["is_sleep"].to_numpy(dtype=int)
    s = np.empty(len(frame), dtype=float)
    s[0] = cfg.initial_s
    for i in range(1, len(frame)):
        dt = max(0.0, times[i] - times[i - 1])
        if labels[i - 1] == 1:
            s[i] = cfg.s_lower + (s[i - 1] - cfg.s_lower) * np.exp(-dt / cfg.tau_sleep_h)
        else:
            s[i] = cfg.s_upper - (cfg.s_upper - s[i - 1]) * np.exp(-dt / cfg.tau_wake_h)
    return s


def score_subject(frame: pd.DataFrame, cfg: ModelConfig) -> tuple[pd.DataFrame, dict]:
    frame = frame.sort_values("timestamp").reset_index(drop=True).copy()
    if len(frame) < 2 or frame["is_sleep"].nunique() < 2:
        raise ValueError("Each subject needs at least two epochs and both classes.")
    phase_ref = infer_midsleep_phase(frame)
    clock_h = (frame["timestamp"].dt.hour + frame["timestamp"].dt.minute / 60
               + frame["timestamp"].dt.second / 3600).to_numpy()
    frame["S"] = homeostatic_process(frame, cfg)
    # C_sleep is positive and maximal at midsleep; it must add to a score where
    # larger values mean greater sleep propensity.
    frame["C_sleep"] = cfg.circadian_amplitude * np.cos(
        2.0 * np.pi * (clock_h - phase_ref) / 24.0
    )
    frame["sleep_propensity"] = frame["S"] + frame["C_sleep"]

    prev = frame["is_sleep"].shift(1)
    onset = frame.index[(prev == 0) & (frame["is_sleep"] == 1)].to_numpy()
    wakeup = frame.index[(prev == 1) & (frame["is_sleep"] == 0)].to_numpy()
    event_idx = np.r_[onset, wakeup]
    event_y = np.r_[np.ones(len(onset)), np.zeros(len(wakeup))]
    transition_auc = (
        float(roc_auc_score(event_y, frame.loc[event_idx, "sleep_propensity"]))
        if len(onset) and len(wakeup) else None
    )
    margin = (
        float(frame.loc[onset, "sleep_propensity"].mean()
              - frame.loc[wakeup, "sleep_propensity"].mean())
        if len(onset) and len(wakeup) else None
    )
    summary = {
        "phase_ref_midsleep_h": phase_ref,
        "epoch_auc_history_conditioned": float(
            roc_auc_score(frame["is_sleep"], frame["sleep_propensity"])
        ),
        "transition_auc_onset_vs_wakeup": transition_auc,
        "transition_margin": margin,
        "n_onset": int(len(onset)),
        "n_wakeup": int(len(wakeup)),
    }
    return frame, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--epoch-output", type=Path, default=Path("two_process_epochs.csv"))
    parser.add_argument("--summary-output", type=Path, default=Path("two_process_summary.json"))
    args = parser.parse_args()

    data = pd.read_csv(args.input_csv)
    required = {"subject_id", "timestamp", "is_sleep"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")
    data["timestamp"] = pd.to_datetime(data["timestamp"], errors="raise")
    if not set(data["is_sleep"].dropna().unique()).issubset({0, 1}):
        raise ValueError("is_sleep must contain only 0 and 1.")

    scored, summaries = [], []
    for subject_id, group in data.groupby("subject_id", sort=False):
        subject_scored, summary = score_subject(group, ModelConfig())
        subject_scored["subject_id"] = subject_id
        summary["subject_id"] = subject_id
        scored.append(subject_scored)
        summaries.append(summary)
    pd.concat(scored, ignore_index=True).to_csv(args.epoch_output, index=False)
    args.summary_output.write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
