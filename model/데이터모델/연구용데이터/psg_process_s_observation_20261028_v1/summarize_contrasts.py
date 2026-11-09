from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 20261028
RESAMPLES = 2000


def summary(values: pd.Series) -> dict:
    array = values.to_numpy(dtype=float)
    rng = np.random.default_rng(SEED)
    means = array[rng.integers(0, len(array), size=(RESAMPLES, len(array)))].mean(axis=1)
    return {
        "mean": float(array.mean()),
        "median": float(np.median(array)),
        "ci95": [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))],
        "n_people": int(len(array)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis", type=Path, required=True)
    args = parser.parse_args()
    frame = pd.read_csv(args.analysis / "participant_metrics.csv")
    output = {
        "schema_version": 1,
        "status": "descriptive_contrasts_requested_during_post_run_review",
        "sign": "positive means the first named comparator has larger error than calibrated probability",
        "supports": {},
    }
    for support in ("primary", "secondary"):
        selected = frame[(frame.support == support) & np.isclose(frame.initial_s, 0.5)]
        rows = {}
        for metric in ("trajectory_mae", "s_end_abs_error", "s_20_abs_error",
                       "onset_abs_difference_min", "wake_abs_difference_min"):
            calibrated = selected[f"classifier_calibrated_probability_{metric}"]
            rows[f"binary_minus_calibrated_{metric}"] = summary(
                selected[f"classifier_binary_{metric}"] - calibrated)
            rows[f"diary_minus_calibrated_{metric}"] = summary(
                selected[f"diary_like_psg_first_to_last_sleep_{metric}"] - calibrated)
            rows[f"raw_probability_minus_calibrated_{metric}"] = summary(
                selected[f"classifier_raw_probability_{metric}"] - calibrated)
        output["supports"][support] = rows
    output["warning"] = "s_20 and transition contrasts are all-awake daytime propagation scenarios, not observed 20:00 current-S or next-night accuracy."
    target = args.analysis / "descriptive_contrasts.json"
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
