#!/usr/bin/env python3
"""Build a conservative 30-second CMI research-only dataset.

CMI event labels are onset/wakeup events, not PSG stages.  This builder never
reads the legacy ``subjects_final`` labels: it derives only high-confidence
sleep and wake intervals from raw events.  All other source samples are
unknown and are excluded rather than silently labelled wake.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

SEED = 20261010
SAMPLES_PER_BIN = 6
SAMPLE_SECONDS = 5
BIN_SECONDS = SAMPLES_PER_BIN * SAMPLE_SECONDS


def sha256_file(path: Path, chunk_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def file_provenance(path: Path) -> dict:
    stat = path.stat()
    return {"path": str(path), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": sha256_file(path)}


def assert_source_schema(path: Path) -> dict[str, str]:
    """Fail before processing if the raw partition cannot encode the protocol grid."""
    schema = pq.ParquetFile(path).schema_arrow
    required = {"series_id", "step", "timestamp", "anglez", "enmo"}
    missing = required - set(schema.names)
    if missing:
        raise ValueError(f"{path.stem}: source schema missing {sorted(missing)}")
    if str(schema.field("step").type) != "uint32":
        raise ValueError(f"{path.stem}: source step must be uint32, got {schema.field('step').type}")
    return {field.name: str(field.type) for field in schema}


def stable_subject_split(subject_ids: Iterable[str], seed: int = SEED) -> dict[str, str]:
    """Stable per-ID hash buckets; adding a subject cannot move existing IDs."""
    ids = sorted({str(subject_id) for subject_id in subject_ids})
    if len(ids) < 7:
        raise ValueError("At least seven subjects are required for a 70/15/15 split")
    assignment: dict[str, str] = {}
    for subject_id in ids:
        bucket = int.from_bytes(hashlib.sha256(f"{seed}:{subject_id}".encode("utf-8")).digest()[:8], "big") / 2**64
        assignment[subject_id] = "train" if bucket < 0.70 else "validation" if bucket < 0.85 else "test"
    return assignment


@dataclass(frozen=True)
class Night:
    night: int
    onset: int
    wakeup: int


def complete_nights(events: pd.DataFrame) -> dict[str, list[Night]]:
    """Keep only exact one-onset/one-wakeup, finite integer, ordered nights."""
    needed = {"series_id", "night", "event", "step"}
    if not needed.issubset(events.columns):
        raise ValueError(f"events missing columns: {sorted(needed - set(events.columns))}")
    frame = events.loc[:, ["series_id", "night", "event", "step"]].copy()
    frame["step_num"] = pd.to_numeric(frame["step"], errors="coerce")
    frame["night_num"] = pd.to_numeric(frame["night"], errors="coerce")
    frame["series_id"] = frame["series_id"].astype(str)
    out: dict[str, list[Night]] = {}
    valid_night = frame["night_num"].notna() & np.isfinite(frame["night_num"]) & np.equal(frame["night_num"], np.floor(frame["night_num"]))
    for (subject_id, night), group in frame.loc[valid_night].groupby(["series_id", "night_num"], sort=True):
        # Validate the whole raw group first: a duplicate plus a missing row
        # cannot silently become an apparently complete night.
        event_counts = group["event"].value_counts().to_dict()
        steps_ok = (
            len(group) == 2 and event_counts == {"onset": 1, "wakeup": 1}
            and group["step_num"].notna().all() and np.isfinite(group["step_num"]).all()
            and np.equal(group["step_num"], np.floor(group["step_num"])).all() and (group["step_num"] >= 0).all()
        )
        if not steps_ok:
            continue
        onset = int(group.loc[group["event"] == "onset", "step_num"].iloc[0])
        wakeup = int(group.loc[group["event"] == "wakeup", "step_num"].iloc[0])
        if onset < wakeup:
            out.setdefault(subject_id, []).append(Night(int(night), onset, wakeup))
    for subject_id in out:
        out[subject_id].sort(key=lambda value: value.night)
    return out


def known_intervals(nights: list[Night]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return [start,end) intervals and labels; unlisted samples are unknown.

    Sleep is [onset, wakeup). Wake is admitted only between complete adjacent
    night IDs, [wakeup_n, onset_n+1), so edges and missing nights remain unknown.
    """
    rows: list[tuple[int, int, int]] = [(night.onset, night.wakeup, 1) for night in nights]
    for current, following in zip(nights, nights[1:]):
        if following.night == current.night + 1 and following.onset > current.wakeup:
            rows.append((current.wakeup, following.onset, 0))
    rows.sort()
    for (_, end, _), (next_start, _, _) in zip(rows, rows[1:]):
        if end > next_start:
            raise ValueError("overlapping derived event intervals")
    if not rows:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64), np.array([], dtype=np.int8)
    starts, ends, labels = zip(*rows)
    return np.asarray(starts, dtype=np.int64), np.asarray(ends, dtype=np.int64), np.asarray(labels, dtype=np.int8)


def labels_for_steps(steps: np.ndarray, starts: np.ndarray, ends: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Map source steps to -1 unknown / 0 wake / 1 sleep without interpolation."""
    result = np.full(len(steps), -1, dtype=np.int8)
    if not len(starts):
        return result
    locations = np.searchsorted(starts, steps, side="right") - 1
    valid = locations >= 0
    candidate = locations[valid]
    inside = steps[valid] < ends[candidate]
    valid_indices = np.flatnonzero(valid)
    result[valid_indices[inside]] = labels[candidate[inside]]
    return result


def aggregate_complete_bins(
    subject_id: str,
    split: str,
    steps: np.ndarray,
    anglez: np.ndarray,
    enmo: np.ndarray,
    labels: np.ndarray,
) -> pd.DataFrame:
    """Aggregate complete six-sample bins anchored to raw step zero.

    A bin is retained only if all six consecutive raw steps are known and both
    sensors are finite.  A 3:3 sleep/wake tie is excluded (never arbitrarily
    assigned); other bins use the majority label and report its purity.
    """
    if not (len(steps) == len(anglez) == len(enmo) == len(labels)):
        raise ValueError("raw arrays have inconsistent lengths")
    columns = [
        "subject_id", "split", "step_start", "relative_time_sec", "available_at_relative_sec",
        "anglez_mean", "anglez_std", "enmo_mean", "enmo_std", "is_sleep", "label_purity", "source_5s_rows",
    ]
    if len(steps) == 0:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame({"step": steps, "anglez": anglez, "enmo": enmo, "label": labels})
    frame = frame.loc[(frame["step"] >= 0) & np.isfinite(frame["anglez"]) & np.isfinite(frame["enmo"])].copy()
    frame["bin"] = (frame["step"] // SAMPLES_PER_BIN).astype("int64")
    grouped = frame.groupby("bin", sort=True).agg(
        source_5s_rows=("step", "size"), step_min=("step", "min"), step_max=("step", "max"),
        label_min=("label", "min"), sleep_count=("label", "sum"),
        anglez_mean=("anglez", "mean"), anglez_std=("anglez", "std"),
        enmo_mean=("enmo", "mean"), enmo_std=("enmo", "std"),
    ).reset_index()
    grouped["step_start"] = grouped["bin"] * SAMPLES_PER_BIN
    complete = (
        (grouped["source_5s_rows"] == SAMPLES_PER_BIN)
        & (grouped["step_min"] == grouped["step_start"])
        & (grouped["step_max"] == grouped["step_start"] + SAMPLES_PER_BIN - 1)
        & (grouped["label_min"] >= 0)
        & (grouped["sleep_count"] * 2 != SAMPLES_PER_BIN)
    )
    grouped = grouped.loc[complete].copy()
    # groupby std uses ddof=1; each retained group has six rows, so this is
    # the equivalent vectorized ddof=0 conversion.
    grouped["anglez_std"] *= np.sqrt((SAMPLES_PER_BIN - 1) / SAMPLES_PER_BIN)
    grouped["enmo_std"] *= np.sqrt((SAMPLES_PER_BIN - 1) / SAMPLES_PER_BIN)
    grouped["subject_id"] = subject_id
    grouped["split"] = split
    grouped["relative_time_sec"] = grouped["step_start"] * SAMPLE_SECONDS
    grouped["available_at_relative_sec"] = (grouped["step_start"] + SAMPLES_PER_BIN) * SAMPLE_SECONDS
    grouped["is_sleep"] = (grouped["sleep_count"] * 2 > SAMPLES_PER_BIN).astype("int8")
    grouped["label_purity"] = np.maximum(grouped["sleep_count"], SAMPLES_PER_BIN - grouped["sleep_count"]) / SAMPLES_PER_BIN
    return grouped.loc[:, columns].reset_index(drop=True)


def event_timestamp_map(events: pd.DataFrame) -> dict[str, dict[int, pd.Timestamp]]:
    """Return finite event timestamps keyed by source step for QC only."""
    required = {"series_id", "night", "event", "step", "timestamp"}
    if not required.issubset(events.columns):
        return {}
    frame = events.loc[:, ["series_id", "step", "timestamp"]].copy()
    frame["step"] = pd.to_numeric(frame["step"], errors="coerce")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True)
    result: dict[str, dict[int, pd.Timestamp]] = {}
    for subject_id, nights in complete_nights(events).items():
        expected_steps = {night.onset for night in nights} | {night.wakeup for night in nights}
        group = frame.loc[(frame["series_id"].astype(str) == subject_id) & frame["step"].isin(expected_steps)].copy()
        if len(group) != len(expected_steps) or group["timestamp"].isna().any():
            raise ValueError(f"{subject_id}: complete event endpoint lacks a parseable timestamp")
        step_times = group.groupby("step")["timestamp"].nunique()
        if (step_times > 1).any():
            raise ValueError(f"{subject_id}: conflicting event timestamps for one step")
        result[subject_id] = {int(step): timestamp for step, timestamp in zip(group["step"], group["timestamp"])}
    return result


def process_subject(
    path: Path,
    nights: list[Night],
    split: str,
    event_times: dict[int, pd.Timestamp] | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Read exactly one source subject at a time; no full-cohort dataframe."""
    subject_id = path.stem
    starts, ends, interval_labels = known_intervals(nights)
    parts: list[pd.DataFrame] = []
    previous_step: int | None = None
    previous_timestamp: pd.Timestamp | None = None
    nonunit_step_pairs = 0
    nonfive_second_timestamp_pairs = 0
    raw_rows = 0
    known_rows = 0
    finite_sensor_rows = 0
    event_timestamp_matches = 0
    event_timestamp_mismatches = 0
    event_timestamp_seen: set[int] = set()
    parquet = pq.ParquetFile(path)
    required = {"series_id", "step", "timestamp", "anglez", "enmo"}
    if not required.issubset(parquet.schema_arrow.names):
        raise ValueError(f"{subject_id}: source schema missing {sorted(required - set(parquet.schema_arrow.names))}")
    for batch in parquet.iter_batches(batch_size=250_000, columns=["series_id", "step", "timestamp", "anglez", "enmo"]):
        frame = batch.to_pandas()
        if not (frame["series_id"].astype(str) == subject_id).all():
            raise ValueError(f"{subject_id}: source series_id mismatch")
        source_steps = pd.to_numeric(frame["step"], errors="raise")
        if source_steps.isna().any() or not np.isfinite(source_steps).all() or not np.equal(source_steps, np.floor(source_steps)).all():
            raise ValueError(f"{subject_id}: source steps must be finite integers")
        steps = source_steps.to_numpy(dtype=np.int64)
        if np.any(steps < 0):
            raise ValueError(f"{subject_id}: negative source step")
        if len(steps):
            if previous_step is not None:
                nonunit_step_pairs += int(steps[0] - previous_step != 1)
            nonunit_step_pairs += int(np.count_nonzero(np.diff(steps) != 1))
            previous_step = int(steps[-1])
        timestamps = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True)
        if timestamps.isna().any():
            raise ValueError(f"{subject_id}: unparseable source timestamp")
        if previous_timestamp is not None:
            nonfive_second_timestamp_pairs += int((timestamps.iat[0] - previous_timestamp).total_seconds() != SAMPLE_SECONDS)
        nonfive_second_timestamp_pairs += int(np.count_nonzero(timestamps.diff().dt.total_seconds().iloc[1:].to_numpy() != SAMPLE_SECONDS))
        previous_timestamp = timestamps.iat[-1]
        anglez = pd.to_numeric(frame["anglez"], errors="raise").to_numpy(dtype=float)
        enmo = pd.to_numeric(frame["enmo"], errors="raise").to_numpy(dtype=float)
        labels = labels_for_steps(steps, starts, ends, interval_labels)
        if event_times:
            candidates = np.flatnonzero(np.isin(steps, np.fromiter(event_times.keys(), dtype=np.int64)))
            for index in candidates:
                step = int(steps[index])
                sensor_time = timestamps.iat[int(index)]
                event_timestamp_seen.add(step)
                if sensor_time == event_times[step]:
                    event_timestamp_matches += 1
                else:
                    event_timestamp_mismatches += 1
        raw_rows += len(frame)
        known_rows += int(np.count_nonzero(labels >= 0))
        finite_sensor_rows += int(np.count_nonzero(np.isfinite(anglez) & np.isfinite(enmo)))
        parts.append(aggregate_complete_bins(subject_id, split, steps, anglez, enmo, labels))
    if nonunit_step_pairs:
        raise ValueError(f"{subject_id}: duplicate or non-consecutive source steps")
    if nonfive_second_timestamp_pairs:
        raise ValueError(f"{subject_id}: source timestamp cadence is not five seconds")
    if event_timestamp_mismatches or len(event_timestamp_seen) != len(event_times or {}):
        raise ValueError(f"{subject_id}: event timestamp does not match source step timestamp")
    # Bins at parquet batch boundaries are intentionally dropped, not reconstructed;
    # this is conservative and avoids retaining an unverified partial sequence.
    dataset = pd.concat(parts, ignore_index=True) if parts else aggregate_complete_bins(subject_id, split, np.array([], dtype=np.int64), np.array([]), np.array([]), np.array([], dtype=np.int8))
    return dataset, {
        "subject_id": subject_id,
        "source_rows": raw_rows,
        "complete_nights": len(nights),
        "derived_known_rows": known_rows,
        "finite_sensor_rows": finite_sensor_rows,
        "nonunit_source_step_pairs": nonunit_step_pairs,
        "nonfive_second_timestamp_pairs": nonfive_second_timestamp_pairs,
        "source_cadence_seconds": SAMPLE_SECONDS,
        "event_timestamp_expected": len(event_times or {}),
        "event_timestamp_seen": len(event_timestamp_seen),
        "event_timestamp_matches": event_timestamp_matches,
        "event_timestamp_mismatches": event_timestamp_mismatches,
        "retained_30s_rows": int(len(dataset)),
        "sleep_30s_rows": int(dataset["is_sleep"].sum()) if len(dataset) else 0,
        "wake_30s_rows": int((dataset["is_sleep"] == 0).sum()) if len(dataset) else 0,
        "input_after": file_provenance(path),
    }


def build_dataset(subjects_dir: Path, events_path: Path, output_dir: Path) -> dict:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output directory: {output_dir}")
    subjects_dir = subjects_dir.resolve(); events_path = events_path.resolve(); output_dir.mkdir(parents=True, exist_ok=True)
    events = pd.read_csv(events_path, usecols=["series_id", "night", "event", "step", "timestamp"])
    nights_by_subject = complete_nights(events)
    event_times_by_subject = event_timestamp_map(events)
    source_paths = {path.stem: path for path in sorted(subjects_dir.glob("*.parquet"))}
    eligible = sorted(set(nights_by_subject) & set(source_paths))
    # Snapshot before the first source read.  A future mutation must prevent
    # a completed build from being represented as one immutable input version.
    input_before = {}
    source_schemas = {}
    for subject_id in eligible:
        source_schemas[subject_id] = assert_source_schema(source_paths[subject_id])
        input_before[subject_id] = file_provenance(source_paths[subject_id])
    splits = stable_subject_split(eligible)
    data_dir = output_dir / "subjects_30s"; data_dir.mkdir()
    qc_rows = []
    output_hashes = {}
    for subject_id in eligible:
        dataset, qc = process_subject(
            source_paths[subject_id], nights_by_subject[subject_id], splits[subject_id], event_times_by_subject.get(subject_id),
        )
        if qc["input_after"] != input_before[subject_id]:
            raise RuntimeError(f"{subject_id}: source changed while being processed")
        qc["input_before"] = input_before[subject_id]
        target = data_dir / f"{subject_id}.parquet"
        dataset.to_parquet(target, index=False)
        qc["output"] = file_provenance(target)
        qc_rows.append(qc); output_hashes[subject_id] = qc["output"]["sha256"]
    qc = pd.DataFrame(qc_rows).sort_values("subject_id")
    qc.to_json(output_dir / "subject_qc_private.json", orient="records", indent=2)
    split_counts = Counter(splits.values())
    public = {
        "dataset": "CMI research-only onset/wakeup-derived sleep-wake bins; not PSG and not naps/stages",
        "native_epoch_seconds": SAMPLE_SECONDS,
        "output_epoch_seconds": BIN_SECONDS,
        "subject_count": int(len(eligible)),
        "split_subject_counts": dict(sorted(split_counts.items())),
        "retained_30s_rows": int(qc["retained_30s_rows"].sum()),
        "sleep_30s_rows": int(qc["sleep_30s_rows"].sum()),
        "wake_30s_rows": int(qc["wake_30s_rows"].sum()),
        "unknown_or_ineligible_source_rows": int(qc["source_rows"].sum() - qc["derived_known_rows"].sum()),
        "nonunit_source_step_pairs": int(qc["nonunit_source_step_pairs"].sum()),
        "nonfive_second_timestamp_pairs": int(qc["nonfive_second_timestamp_pairs"].sum()),
        "event_timestamp_mismatches": int(qc["event_timestamp_mismatches"].sum()),
        "tie_policy": "3_sleep_3_wake bins excluded",
        "unknown_policy": "edges, missing-night gaps, incomplete nights, and non-finite sensors excluded",
        "feature_policy": "only current-bin anglez/enmo mean/std; no label-derived, step, relative-time, clock, or future features",
    }
    provenance = {
        "events": file_provenance(events_path),
        "subjects_dir": str(subjects_dir),
        "source_schemas": source_schemas,
        "eligible_subjects": len(eligible),
        "output_subject_sha256": output_hashes,
        "protocol": public,
        "builder": file_provenance(Path(__file__).resolve()),
        "environment": {"python": sys.version, "platform": platform.platform(), "numpy": np.__version__, "pandas": pd.__version__},
    }
    (output_dir / "public_aggregate.json").write_text(json.dumps(public, indent=2, ensure_ascii=False), encoding="utf-8")
    (output_dir / "provenance_private.json").write_text(json.dumps(provenance, indent=2, ensure_ascii=False), encoding="utf-8")
    (output_dir / "split_manifest_private.json").write_text(json.dumps(splits, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return public


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subjects-dir", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_dataset(args.subjects_dir, args.events, args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
