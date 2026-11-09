#!/usr/bin/env python3
"""Train leakage-controlled bedtime/wake residual Ridge models and export Kotlin JSON."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error
from sklearn.preprocessing import StandardScaler


# Decision-time features only. Additions require an explicit schema/version change.
DEFAULT_FEATURES = [
    "current_s",
    "current_c",
    "current_propensity",
    "day_hr_mean_pre20",
    "day_hr_std_pre20",
    "late_hr_mean_pre20",
    "steps_pre20",
    "active_minutes_pre20",
    "previous_sleep_duration_h",
    "prior_bedtime_median_7",
    "prior_wake_median_7",
    "prior_duration_median_7",
    "caffeine_mg",
    "caffeine_gap_min",
    "nap_minutes",
    "nap_end_gap_min",
    "exercise_minutes",
    "exercise_intensity",
    "fatigue_score",
    "target_wake_minute",
    "weekend",
]

REQUIRED_TARGETS = [
    "actual_bedtime_minute",
    "actual_wake_minute",
    "physical_bedtime_minute",
    "physical_wake_minute",
]


@dataclass
class FitBundle:
    imputer: SimpleImputer
    scaler: StandardScaler
    bedtime: Ridge
    wake: Ridge


def circular_residual(actual: pd.Series, baseline: pd.Series) -> np.ndarray:
    """Signed clock-time difference in [-720, 720) minutes."""
    return ((actual.to_numpy(float) - baseline.to_numpy(float) + 720.0) % 1440.0) - 720.0


def split_by_date(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = np.array(sorted(frame["date"].drop_duplicates()))
    if len(dates) < 30:
        raise ValueError("최소 30개의 서로 다른 날짜가 필요합니다")
    train_end = max(1, int(len(dates) * 0.70))
    val_end = max(train_end + 1, int(len(dates) * 0.85))
    val_end = min(val_end, len(dates) - 1)
    return (
        frame[frame["date"].isin(dates[:train_end])].copy(),
        frame[frame["date"].isin(dates[train_end:val_end])].copy(),
        frame[frame["date"].isin(dates[val_end:])].copy(),
    )


def fit_bundle(frame: pd.DataFrame, features: list[str], alpha: float) -> FitBundle:
    x = frame[features]
    y_bed = circular_residual(frame["actual_bedtime_minute"], frame["physical_bedtime_minute"])
    y_wake = circular_residual(frame["actual_wake_minute"], frame["physical_wake_minute"])
    imputer = SimpleImputer(strategy="median").fit(x)
    scaler = StandardScaler().fit(imputer.transform(x))
    z = scaler.transform(imputer.transform(x))
    return FitBundle(
        imputer=imputer,
        scaler=scaler,
        bedtime=Ridge(alpha=alpha).fit(z, y_bed),
        wake=Ridge(alpha=alpha).fit(z, y_wake),
    )


def predict(bundle: FitBundle, frame: pd.DataFrame, features: list[str]) -> tuple[np.ndarray, np.ndarray]:
    z = bundle.scaler.transform(bundle.imputer.transform(frame[features]))
    return bundle.bedtime.predict(z), bundle.wake.predict(z)


def target_residuals(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    return (
        circular_residual(frame["actual_bedtime_minute"], frame["physical_bedtime_minute"]),
        circular_residual(frame["actual_wake_minute"], frame["physical_wake_minute"]),
    )


def train_and_export(
    input_csv: Path,
    output_json: Path,
    features: list[str] | None = None,
    alphas: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0),
) -> dict:
    features = list(features or DEFAULT_FEATURES)
    if len(features) != len(set(features)):
        raise ValueError("feature 목록에 중복이 있습니다")
    forbidden = set(REQUIRED_TARGETS) | {"date", "savedAt", "saved_at"}
    overlap = forbidden.intersection(features)
    if overlap:
        raise ValueError(f"미래/정답 열을 feature로 사용할 수 없습니다: {sorted(overlap)}")

    frame = pd.read_csv(input_csv)
    required = {"date", *features, *REQUIRED_TARGETS}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"필수 열 누락: {missing}")
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.date.astype(str)
    frame = frame.sort_values(["date"] + (["participant"] if "participant" in frame else [])).reset_index(drop=True)
    frame = frame.dropna(subset=REQUIRED_TARGETS)
    train, val, test = split_by_date(frame)

    best_alpha = None
    best_score = float("inf")
    validation_scores: dict[str, dict[str, float]] = {}
    for alpha in alphas:
        candidate = fit_bundle(train, features, alpha)
        pb, pw = predict(candidate, val, features)
        yb, yw = target_residuals(val)
        bed_mae = mean_absolute_error(yb, pb)
        wake_mae = mean_absolute_error(yw, pw)
        validation_scores[str(alpha)] = {"bedtime_mae_min": bed_mae, "wake_mae_min": wake_mae}
        score = (bed_mae + wake_mae) / 2.0
        if score < best_score:
            best_score, best_alpha = score, alpha

    development = pd.concat([train, val], ignore_index=True)
    final = fit_bundle(development, features, float(best_alpha))
    test_bed, test_wake = predict(final, test, features)
    test_y_bed, test_y_wake = target_residuals(test)

    def numbers(values) -> list[float]:
        return [float(v) for v in np.asarray(values).ravel()]

    payload = {
        "schema_version": 1,
        "model_version": f"ridge-residual-{pd.Timestamp.utcnow().strftime('%Y%m%d')}",
        "status": "trained",
        "feature_order": features,
        "preprocessing": {
            "imputation_median": numbers(final.imputer.statistics_),
            "mean": numbers(final.scaler.mean_),
            "scale": numbers(final.scaler.scale_),
        },
        "targets": {
            "bedtime_residual_min": {
                "intercept": float(final.bedtime.intercept_),
                "coefficients": numbers(final.bedtime.coef_),
            },
            "wake_residual_min": {
                "intercept": float(final.wake.intercept_),
                "coefficients": numbers(final.wake.coef_),
            },
        },
        "training": {
            "alpha": float(best_alpha),
            "split": "chronological_unique_date_70_15_15",
            "development_rows": int(len(development)),
            "test_rows": int(len(test)),
            "development_last_date": str(development["date"].max()),
            "test_first_date": str(test["date"].min()),
            "validation_scores": validation_scores,
            "test_metrics": {
                "bedtime_mae_min": float(mean_absolute_error(test_y_bed, test_bed)),
                "wake_mae_min": float(mean_absolute_error(test_y_wake, test_wake)),
            },
        },
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True, help="설문+Watch+물리기준이 결합된 일별 CSV")
    parser.add_argument("--output", type=Path, required=True, help="Kotlin assets에 넣을 JSON")
    args = parser.parse_args()
    payload = train_and_export(args.input, args.output)
    print(json.dumps(payload["training"]["test_metrics"], ensure_ascii=False))


if __name__ == "__main__":
    main()
