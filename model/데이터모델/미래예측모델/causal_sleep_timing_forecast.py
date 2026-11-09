#!/usr/bin/env python3
"""Leakage-free bedtime and next-morning wake-time forecast.

Prediction time is fixed at 20:00 local time on forecast_date.  Every feature
is calculated using data available no later than that cutoff.  The target is
the principal sleep episode beginning between 20:00 and 12:00 the next day.

This is deliberately a small-data model: temporal 70/15/15 split, validation
selection, and a final untouched test period.  It does not use sleep labels,
sensor samples, or day aggregates after the 20:00 prediction cutoff.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from sklearn.dummy import DummyRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


CUTOFF_HOUR = 20
TARGET_WINDOW_HOURS = 16  # 20:00 to noon next day


def hours_from_cutoff(value: pd.Series, forecast_date: pd.Series) -> pd.Series:
    cutoff = pd.to_datetime(forecast_date) + pd.Timedelta(hours=CUTOFF_HOUR)
    return (pd.to_datetime(value).reset_index(drop=True) - cutoff.reset_index(drop=True)).dt.total_seconds() / 3600


def circular_history(values: pd.Series, window: int = 7) -> pd.Series:
    """Past-only rolling median; shift prevents a row's own target entering X."""
    return values.shift(1).rolling(window, min_periods=1).median()


def make_night_targets(episodes: pd.DataFrame) -> pd.DataFrame:
    e = episodes.loc[episodes["is_main_sleep"] & episodes["episode_qc_pass"]].copy()
    e["sleep_start"] = pd.to_datetime(e["sleep_start"])
    e["sleep_end"] = pd.to_datetime(e["sleep_end"])
    # A 00:00--11:59 start belongs to the preceding evening's forecast.
    e["forecast_date"] = e["sleep_start"].dt.normalize()
    e.loc[e["sleep_start"].dt.hour < CUTOFF_HOUR, "forecast_date"] -= pd.Timedelta(days=1)
    cutoff = e["forecast_date"] + pd.Timedelta(hours=CUTOFF_HOUR)
    delta_h = (e["sleep_start"] - cutoff).dt.total_seconds() / 3600
    e = e.loc[(delta_h >= 0) & (delta_h <= TARGET_WINDOW_HOURS)].copy()
    # If wearable data reports a short evening nap as well, retain the longest
    # overnight candidate for that prediction date.
    e = e.sort_values(["forecast_date", "episode_interval_min"], ascending=[True, False])
    e = e.drop_duplicates("forecast_date", keep="first").sort_values("forecast_date").reset_index(drop=True)
    e["bedtime_hours"] = (e["sleep_start"] - (e["forecast_date"] + pd.Timedelta(hours=CUTOFF_HOUR))).dt.total_seconds() / 3600
    e["wake_hours"] = (e["sleep_end"] - (e["forecast_date"] + pd.Timedelta(hours=CUTOFF_HOUR))).dt.total_seconds() / 3600
    return e


def aggregate_before_cutoff(targets: pd.DataFrame, hr: pd.DataFrame, steps: pd.DataFrame) -> pd.DataFrame:
    hr = hr.copy(); hr["timestamp"] = pd.to_datetime(hr["timestamp"])
    steps = steps.copy(); steps["timestamp"] = pd.to_datetime(steps["timestamp"])
    rows = []
    for _, target in targets.iterrows():
        day = target.forecast_date
        cutoff = day + pd.Timedelta(hours=CUTOFF_HOUR)
        day_start = day
        # Strictly before 20:00: sensor values at/after the decision time are excluded.
        h = hr.loc[(hr.timestamp >= day_start) & (hr.timestamp < cutoff), "heart_rate"].dropna()
        h_recent = hr.loc[(hr.timestamp >= cutoff - pd.Timedelta(hours=3)) & (hr.timestamp < cutoff), "heart_rate"].dropna()
        st = steps.loc[(steps.timestamp >= day_start) & (steps.timestamp < cutoff)].copy()
        st = st.loc[st.get("steps_qc_pass", True)]
        row = target.to_dict()
        row.update({
            "weekday": day.weekday(),
            "weekend": int(day.weekday() >= 5),
            "day_hr_mean_pre20": h.mean(), "day_hr_std_pre20": h.std(),
            "day_hr_min_pre20": h.min(), "day_hr_max_pre20": h.max(), "day_hr_n_pre20": len(h),
            "late_hr_mean_pre20": h_recent.mean(), "late_hr_n_pre20": len(h_recent),
            "steps_pre20": st["steps_10min"].sum(min_count=1),
            "active_bins_pre20": (st["steps_10min"] > 0).sum(),
            "step_bins_pre20": len(st),
        })
        rows.append(row)
    return pd.DataFrame(rows)


def add_available_history(frame: pd.DataFrame) -> pd.DataFrame:
    d = frame.sort_values("forecast_date").copy()
    d["prior_bedtime_median_7"] = circular_history(d["bedtime_hours"])
    d["prior_wake_median_7"] = circular_history(d["wake_hours"])
    d["prior_duration_median_7"] = circular_history(d["episode_interval_min"])
    # The previous labelled episode is already complete by the next 20:00 cutoff.
    d["previous_sleep_duration_h"] = d["episode_interval_min"].shift(1) / 60.0
    return d


def split_temporally(frame: pd.DataFrame) -> pd.Series:
    n = len(frame)
    train_end, valid_end = int(n * .70), int(n * .85)
    split = np.full(n, "test", dtype=object)
    split[:train_end] = "train"; split[train_end:valid_end] = "validation"
    return pd.Series(split, index=frame.index)


def candidate_models() -> dict:
    return {
        "training_median_baseline": DummyRegressor(strategy="median"),
        "ridge": make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(), Ridge(alpha=10.0)),
        "lightgbm": LGBMRegressor(
            n_estimators=80, learning_rate=.04, num_leaves=7, max_depth=3,
            min_child_samples=16, reg_lambda=5.0, reg_alpha=1.0,
            random_state=42, verbosity=-1,
        ),
    }


def evaluate_target(frame: pd.DataFrame, features: list[str], target: str) -> tuple[pd.DataFrame, dict, object]:
    train = frame.query("split == 'train'"); valid = frame.query("split == 'validation'")
    test = frame.query("split == 'test'")
    candidates = candidate_models()
    validation = {}
    for name, model in candidates.items():
        model.fit(train[features], train[target])
        pred = model.predict(valid[features])
        validation[name] = float(mean_absolute_error(valid[target], pred))
    chosen = min(validation, key=validation.get)
    model = candidate_models()[chosen]
    fitted = pd.concat([train, valid])
    model.fit(fitted[features], fitted[target])
    pred = model.predict(test[features])
    baseline = DummyRegressor(strategy="median").fit(fitted[features], fitted[target])
    baseline_pred = baseline.predict(test[features])
    out = test[["forecast_date", target]].copy()
    out["prediction_hours"] = pred
    out["absolute_error_min"] = abs(out[target] - pred) * 60
    metrics = {
        "selected_model_by_validation_mae": chosen,
        "validation_mae_hours": validation,
        "test_n": int(len(test)),
        "test_mae_minutes": float(mean_absolute_error(test[target], pred) * 60),
        "test_training_median_baseline_mae_minutes": float(mean_absolute_error(test[target], baseline_pred) * 60),
        "test_rmse_minutes": float(mean_squared_error(test[target], pred) ** .5 * 60),
        "test_median_ae_minutes": float(out["absolute_error_min"].median()),
    }
    return out, metrics, model


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", type=Path, required=True)
    p.add_argument("--heart-rate", type=Path, required=True)
    p.add_argument("--steps", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args(); a.output_dir.mkdir(parents=True, exist_ok=True)
    targets = make_night_targets(pd.read_parquet(a.episodes))
    data = aggregate_before_cutoff(targets, pd.read_parquet(a.heart_rate), pd.read_parquet(a.steps))
    data = add_available_history(data).sort_values("forecast_date").reset_index(drop=True)
    data["split"] = split_temporally(data)
    features = [
        "weekday", "weekend", "day_hr_mean_pre20", "day_hr_std_pre20", "day_hr_min_pre20", "day_hr_max_pre20", "day_hr_n_pre20",
        "late_hr_mean_pre20", "late_hr_n_pre20", "steps_pre20", "active_bins_pre20", "step_bins_pre20",
        "prior_bedtime_median_7", "prior_wake_median_7", "prior_duration_median_7", "previous_sleep_duration_h",
    ]
    bedtime_pred, bedtime_metrics, _ = evaluate_target(data, features, "bedtime_hours")
    wake_pred, wake_metrics, _ = evaluate_target(data, features, "wake_hours")
    result = bedtime_pred.merge(wake_pred[["forecast_date", "wake_hours", "prediction_hours", "absolute_error_min"]], on="forecast_date", suffixes=("_bedtime", "_wake"))
    result.to_csv(a.output_dir / "held_out_test_predictions.csv", index=False)
    data.to_parquet(a.output_dir / "causal_daily_features.parquet", index=False)
    report = {
        "prediction_protocol": "At 20:00 local time, use only records earlier than 20:00 to predict the principal sleep episode that begins by noon next day.",
        "leakage_guards": ["No sensor data at or after cutoff", "No same-night sleep labels used as feature", "Past-target rolling statistics are shifted by one night", "Chronological 70/15/15 split; model selected only on validation, test used once"],
        "n_total": int(len(data)), "split_counts": data.split.value_counts().to_dict(),
        "features": features, "bedtime": bedtime_metrics, "wake": wake_metrics,
    }
    (a.output_dir / "validation_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
