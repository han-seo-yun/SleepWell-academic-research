#!/usr/bin/env python3
"""Build a causal Samsung sensor-feature cache at fixed 20:00 KST cutoffs.

The processed sensor timestamps are local wall-clock Asia/Seoul values.  A bin
is treated as retrospectively available only after its full one- or ten-minute
interval has ended.  The source exports do not contain receipt timestamps.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


KST = ZoneInfo("Asia/Seoul")
FEATURES = [
    "hr_mean", "hr_std", "hr_min", "hr_max", "hr_count",
    "late_hr_mean", "late_hr_count", "steps", "step_bins",
    "active_step_bins",
]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _kst_series(values: pd.Series, *, naive_is_local: bool) -> pd.Series:
    """Normalize timestamps without allowing the host timezone to intervene."""
    individual = [pd.Timestamp(value) for value in values]
    aware = [value.tzinfo is not None for value in individual]
    if any(aware) and not all(aware):
        raise ValueError("Timestamp series cannot mix offset-aware and naive values")
    if not any(aware):
        if not naive_is_local:
            raise ValueError("Cutoffs must carry an explicit UTC offset")
        parsed = pd.Series(pd.DatetimeIndex(individual), index=values.index)
        return parsed.dt.tz_localize(KST, ambiguous="raise", nonexistent="raise")
    parsed = pd.Series(pd.to_datetime(values, errors="raise", utc=True), index=values.index)
    return parsed.dt.tz_convert(KST)


def _finite_values(frame: pd.DataFrame, column: str) -> pd.Series:
    values = pd.to_numeric(frame[column], errors="coerce")
    return values.loc[np.isfinite(values)]


def build_cache(
    episodes: pd.DataFrame,
    heart_rate: pd.DataFrame,
    steps: pd.DataFrame,
    *,
    expected_rows: int | None = None,
) -> pd.DataFrame:
    """Aggregate same-day sensors whose complete bins end by each cutoff."""
    required_episode = {"cutoff"}
    required_hr = {"timestamp", "heart_rate", "hr_qc_pass"}
    required_steps = {"timestamp", "steps_10min", "steps_qc_pass"}
    for name, frame, required in (
        ("episodes", episodes, required_episode),
        ("heart_rate", heart_rate, required_hr),
        ("steps", steps, required_steps),
    ):
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"{name} missing columns: {sorted(missing)}")

    cutoffs = _kst_series(episodes["cutoff"], naive_is_local=False)
    if cutoffs.duplicated().any():
        raise ValueError("Episode cutoffs must be unique")
    if not ((cutoffs.dt.hour == 20) & (cutoffs.dt.minute == 0)
            & (cutoffs.dt.second == 0) & (cutoffs.dt.microsecond == 0)).all():
        raise ValueError("Every decision cutoff must be exactly 20:00 Asia/Seoul")
    cutoffs = cutoffs.sort_values(ignore_index=True)
    if expected_rows is not None and len(cutoffs) != expected_rows:
        raise ValueError(f"Expected {expected_rows} fixed cutoffs, found {len(cutoffs)}")

    hr = heart_rate.loc[heart_rate["hr_qc_pass"].fillna(False).astype(bool)].copy()
    st = steps.loc[steps["steps_qc_pass"].fillna(False).astype(bool)].copy()
    hr["timestamp"] = _kst_series(hr["timestamp"], naive_is_local=True)
    st["timestamp"] = _kst_series(st["timestamp"], naive_is_local=True)

    rows: list[dict[str, object]] = []
    for cutoff in cutoffs:
        day_start = cutoff.normalize()
        h = hr.loc[
            (hr["timestamp"] >= day_start)
            & (hr["timestamp"] + pd.Timedelta(minutes=1) <= cutoff)
        ]
        s = st.loc[
            (st["timestamp"] >= day_start)
            & (st["timestamp"] + pd.Timedelta(minutes=10) <= cutoff)
        ]
        hv = _finite_values(h, "heart_rate")
        late = _finite_values(
            h.loc[h["timestamp"] >= cutoff - pd.Timedelta(hours=3)],
            "heart_rate",
        )
        step_values = pd.to_numeric(s["steps_10min"], errors="coerce")
        finite_steps = step_values.loc[np.isfinite(step_values)]
        rows.append({
            "cutoff": cutoff.isoformat(),
            "hr_mean": hv.mean(),
            "hr_std": hv.std(),
            "hr_min": hv.min(),
            "hr_max": hv.max(),
            "hr_count": int(len(hv)),
            "late_hr_mean": late.mean(),
            "late_hr_count": int(len(late)),
            "steps": finite_steps.sum(min_count=1),
            "step_bins": int(len(finite_steps)),
            "active_step_bins": int((finite_steps > 0).sum()),
        })
    return pd.DataFrame(rows, columns=["cutoff", *FEATURES])


def write_cache(
    episodes_path: Path,
    personal_output: Path,
    output_dir: Path,
    *,
    expected_rows: int = 196,
) -> dict:
    hr_path = personal_output / "heart_rate_1min.parquet"
    steps_path = personal_output / "steps_10min.parquet"
    for path in (episodes_path, hr_path, steps_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    result = build_cache(
        pd.read_parquet(episodes_path),
        pd.read_parquet(hr_path),
        pd.read_parquet(steps_path),
        expected_rows=expected_rows,
    )
    parquet_path = output_dir / "features.parquet"
    csv_path = output_dir / "features.csv"
    result.to_parquet(parquet_path, index=False)
    result.to_csv(csv_path, index=False)

    coverage = {
        feature: {
            "nonmissing_rows": int(result[feature].notna().sum()),
            "missing_rows": int(result[feature].isna().sum()),
        }
        for feature in FEATURES
    }
    metadata = {
        "artifact_type": "sleepwell_personal_pre20_sensor_cache",
        "version": "personal_pre20_v2",
        "decision_time": "20:00",
        "timezone": "Asia/Seoul",
        "rows": int(len(result)),
        "features": FEATURES,
        "availability_assumption": (
            "Retrospective end-of-bin availability: a 1-minute heart-rate bin "
            "or 10-minute step bin is available only when its entire interval "
            "ends at or before the cutoff; source tables have no receipt time."
        ),
        "window": "local calendar-day start through 20:00 cutoff",
        "source_sha256": {
            "episodes": digest(episodes_path),
            "heart_rate_1min": digest(hr_path),
            "steps_10min": digest(steps_path),
        },
        "artifact_sha256": {
            "features_parquet": digest(parquet_path),
            "features_csv": digest(csv_path),
        },
        "coverage": coverage,
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episodes", required=True, type=Path)
    parser.add_argument("--personal-output", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-rows", type=int, default=196)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    metadata = write_cache(
        args.episodes,
        args.personal_output,
        args.output,
        expected_rows=args.expected_rows,
    )
    print(json.dumps({"output": str(args.output), "rows": metadata["rows"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
