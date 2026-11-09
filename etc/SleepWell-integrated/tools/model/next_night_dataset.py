#!/usr/bin/env python3
"""Build leakage-bounded next-night sleep episodes from existing local cohorts.

All timestamps are local wall-clock values represented with a fixed +09:00
offset so the three cohorts share one serialization contract.  The offset does
not assert that CMI or MESA participants were located in Korea.  Offline target
availability is conservatively recorded as the episode wake time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


FIXED_OFFSET = timezone(timedelta(hours=9))
OUTPUT_COLUMNS = [
    "cohort", "subject_id", "split", "cutoff", "bed", "wake", "observed_at",
    "bedtime_min", "wake_min", "duration_min", "age", "sex", "label_source",
]
MESA_TOKEN_NAMESPACE = "mesa_subject_v1"


def sha256_file(path: Path, chunk_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def source_record(path: Path, sha256: str | None = None) -> dict:
    stat = path.stat()
    return {
        "path": str(path), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256 or sha256_file(path),
    }


def mesa_subject_token(participant: str | int) -> str:
    normalized = str(int(str(participant).strip()))
    digest = hashlib.sha256(f"{MESA_TOKEN_NAMESPACE}|{normalized}".encode("utf-8")).hexdigest()
    return f"mesa_{digest[:20]}"


def local_wall_timestamp(value) -> pd.Timestamp:
    """Preserve displayed date/time fields and attach the shared fixed offset."""
    if value is None or pd.isna(value):
        return pd.NaT
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return pd.NaT
    parsed = parsed.replace(tzinfo=None).replace(tzinfo=FIXED_OFFSET)
    return pd.Timestamp(parsed)


def _attach_fixed_offset(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        return pd.NaT
    if stamp.tzinfo is not None:
        stamp = stamp.tz_localize(None)
    return stamp.tz_localize(FIXED_OFFSET)


def _cutoff_for(bed: pd.Timestamp) -> pd.Timestamp:
    cutoff = bed.normalize() + pd.Timedelta(hours=20)
    if bed < cutoff:
        cutoff -= pd.Timedelta(days=1)
    return cutoff


def finalize_episodes(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Apply the common target window and retain the longest episode per night."""
    counts = Counter(input_rows=int(len(frame)))
    if frame.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS), dict(counts)
    data = frame.copy()
    for column in ("bed", "wake"):
        if column not in data:
            raise ValueError(f"missing required episode column: {column}")
    data = data.loc[data["bed"].notna() & data["wake"].notna()].copy()
    counts["missing_time_rows"] = int(len(frame) - len(data))
    data = data.loc[data["wake"] > data["bed"]].copy()
    counts["nonpositive_rows"] = int(counts["input_rows"] - counts["missing_time_rows"] - len(data))
    data["cutoff"] = data["bed"].map(_cutoff_for)
    data["bedtime_min"] = (data["bed"] - data["cutoff"]).dt.total_seconds() / 60.0
    data["wake_min"] = (data["wake"] - data["cutoff"]).dt.total_seconds() / 60.0
    data["duration_min"] = (data["wake"] - data["bed"]).dt.total_seconds() / 60.0
    valid = (
        data["bedtime_min"].between(0, 960, inclusive="both")
        & data["wake_min"].le(1440)
        & data["duration_min"].between(30, 960, inclusive="both")
    )
    counts["outside_common_window_rows"] = int((~valid).sum())
    data = data.loc[valid].copy()
    before_dedup = len(data)
    data = (
        data.sort_values(["cohort", "subject_id", "cutoff", "duration_min", "bed"],
                         ascending=[True, True, True, False, True])
        .drop_duplicates(["cohort", "subject_id", "cutoff"], keep="first")
        .sort_values(["cohort", "subject_id", "cutoff"])
        .reset_index(drop=True)
    )
    counts["duplicate_night_rows"] = int(before_dedup - len(data))
    counts["output_rows"] = int(len(data))
    data["observed_at"] = data["wake"]
    for column in ("age", "sex"):
        if column not in data:
            data[column] = np.nan if column == "age" else pd.NA
    return data.loc[:, OUTPUT_COLUMNS], dict(sorted(counts.items()))


def build_cmi_episodes(events: pd.DataFrame, splits: dict[str, str]) -> tuple[pd.DataFrame, dict]:
    required = {"series_id", "night", "event", "timestamp"}
    if not required.issubset(events.columns):
        raise ValueError(f"CMI events missing columns: {sorted(required - set(events.columns))}")
    frame = events.loc[events["series_id"].astype(str).isin(splits), list(required)].copy()
    frame["series_id"] = frame["series_id"].astype(str)
    rows = []
    qc = Counter(source_rows=int(len(events)), eligible_subject_rows=int(len(frame)))
    for (subject, night), group in frame.groupby(["series_id", "night"], sort=True, dropna=False):
        qc["night_groups"] += 1
        event_counts = group["event"].value_counts(dropna=False).to_dict()
        if len(group) != 2 or event_counts != {"onset": 1, "wakeup": 1}:
            qc["invalid_pair_groups"] += 1
            continue
        bed = local_wall_timestamp(group.loc[group["event"] == "onset", "timestamp"].iloc[0])
        wake = local_wall_timestamp(group.loc[group["event"] == "wakeup", "timestamp"].iloc[0])
        if pd.isna(bed) or pd.isna(wake):
            qc["invalid_timestamp_groups"] += 1
            continue
        rows.append({
            "cohort": "CMI", "subject_id": subject, "split": splits[subject],
            "bed": bed, "wake": wake, "age": np.nan, "sex": pd.NA,
            "label_source": "CMI_sleep_event_exact_pair",
        })
    result, common_qc = finalize_episodes(pd.DataFrame(rows))
    qc.update({f"common_{key}": value for key, value in common_qc.items()})
    qc["subjects"] = int(result["subject_id"].nunique()) if len(result) else 0
    return result, dict(sorted(qc.items()))


def build_samsung_episodes(raw: pd.DataFrame, reference: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    required = {"sleep_start", "sleep_end", "episode_interval_min", "is_main_sleep", "episode_qc_pass"}
    if not required.issubset(raw.columns):
        raise ValueError(f"Samsung episodes missing columns: {sorted(required - set(raw.columns))}")
    if not {"cutoff", "split"}.issubset(reference.columns):
        raise ValueError("Samsung reference must contain cutoff and split")
    eligible = raw.loc[raw["is_main_sleep"].eq(True) & raw["episode_qc_pass"].eq(True)].copy()
    eligible["bed"] = eligible["sleep_start"].map(_attach_fixed_offset)
    eligible["wake"] = eligible["sleep_end"].map(_attach_fixed_offset)
    provisional = pd.DataFrame({
        "cohort": "Samsung", "subject_id": "samsung_personal_001", "split": "",
        "bed": eligible["bed"], "wake": eligible["wake"], "age": np.nan, "sex": pd.NA,
        "label_source": "consumer_device_episode_boundary",
    })
    result, common_qc = finalize_episodes(provisional)
    ref = reference.loc[:, ["cutoff", "split"]].copy()
    ref["cutoff"] = ref["cutoff"].map(_attach_fixed_offset)
    if ref["cutoff"].duplicated().any():
        raise ValueError("Samsung reference contains duplicate cutoffs")
    result = result.drop(columns="split").merge(ref, on="cutoff", how="inner", validate="one_to_one")
    result = result.loc[:, OUTPUT_COLUMNS].sort_values("cutoff").reset_index(drop=True)
    if len(result) != len(reference):
        raise ValueError(f"Samsung reference preservation failed: {len(result)} != {len(reference)}")
    qc = {
        "source_rows": int(len(raw)), "main_qc_rows": int(len(eligible)),
        "reference_rows": int(len(reference)), "output_rows": int(len(result)),
        **{f"common_{key}": value for key, value in common_qc.items()},
    }
    return result, dict(sorted(qc.items()))


def extract_mesa_episodes(
    path: Path, participant: str, split: str, age: float | None, sex: str | None,
) -> tuple[pd.DataFrame, dict, str]:
    """Extract continuous on-wrist REST-S bouts and ACT-derived sleep bounds."""
    rows: list[dict] = []
    qc = Counter()
    required = ["mesaid", "line", "linetime", "date_shifted", "offwrist", "wake", "interval"]
    try:
        frame = pd.read_csv(path, usecols=required, low_memory=False)
    except ValueError as error:
        raise ValueError(f"{path.name}: missing MESA columns") from error
    qc["source_rows"] = int(len(frame))
    content_id = pd.to_numeric(frame["mesaid"], errors="coerce")
    if content_id.notna().any() and not content_id.dropna().astype("int64").eq(int(participant)).all():
        raise ValueError(f"{path.name}: participant mismatch")
    lines = pd.to_numeric(frame["line"], errors="coerce")
    times = pd.to_datetime(
        frame["date_shifted"].astype("string") + " " + frame["linetime"].astype("string"), errors="coerce",
    ).dt.tz_localize(FIXED_OFFSET)
    wakes = pd.to_numeric(frame["wake"], errors="coerce")
    identity_time_valid = content_id.notna() & lines.notna() & times.notna()
    qc["invalid_identity_or_time_rows"] = int((~identity_time_valid).sum())
    cadence = lines.diff().eq(1) & times.diff().eq(pd.Timedelta(seconds=30))
    qc["cadence_breaks"] = int((identity_time_valid & identity_time_valid.shift(fill_value=False) & ~cadence).sum())
    eligible = (
        identity_time_valid & frame["interval"].astype("string").str.strip().eq("REST-S")
        & pd.to_numeric(frame["offwrist"], errors="coerce").eq(0) & wakes.isin([0, 1])
    )
    qc["nonrest_offwrist_or_invalid_rows"] = int((~eligible).sum())
    continues = eligible & eligible.shift(fill_value=False) & cadence
    groups = pd.Series(np.cumsum((~continues).to_numpy(dtype=bool)), index=frame.index)
    eligible_frame = pd.DataFrame({"stamp": times, "wake_value": wakes, "group": groups}).loc[eligible]
    for _, group in eligible_frame.groupby("group", sort=True):
        qc["rest_segments"] += 1
        sleep_times = group.loc[group["wake_value"].eq(0), "stamp"]
        if sleep_times.empty:
            qc["rest_segments_without_sleep"] += 1
            continue
        rows.append({
            "cohort": "MESA", "subject_id": mesa_subject_token(participant), "split": split,
            "bed": sleep_times.iloc[0], "wake": sleep_times.iloc[-1] + pd.Timedelta(seconds=30),
            "age": age, "sex": sex, "label_source": "MESA_ACT_wake_within_rest_weak_label",
        })
    result, common_qc = finalize_episodes(pd.DataFrame(rows))
    qc.update({f"common_{key}": value for key, value in common_qc.items()})
    return result, dict(sorted(qc.items())), sha256_file(path)


def build_mesa_episodes(
    actigraphy_dir: Path, manifest: dict, demographics: pd.DataFrame,
) -> tuple[pd.DataFrame, dict, list[dict]]:
    required_demo = {"nsrrid", "nsrr_age", "nsrr_sex"}
    if not required_demo.issubset(demographics.columns):
        raise ValueError(f"MESA demographics missing columns: {sorted(required_demo - set(demographics.columns))}")
    demo = demographics.loc[:, list(required_demo)].copy()
    demo["participant"] = pd.to_numeric(demo["nsrrid"], errors="coerce").astype("Int64").astype(str)
    if demo["participant"].duplicated().any():
        raise ValueError("MESA demographics contains duplicate participant IDs")
    demo = demo.set_index("participant")
    manifest_subjects = {item["token"]: item for item in manifest.get("subjects", [])}
    if len(manifest_subjects) != 2139:
        raise ValueError(f"expected fixed 2,139-subject MESA manifest, got {len(manifest_subjects)}")
    outputs, sources = [], []
    aggregate = Counter(manifest_subjects=len(manifest_subjects))
    for path in sorted(actigraphy_dir.glob("mesa-sleep-*.csv")):
        suffix = path.stem.rsplit("-", 1)[-1]
        if not suffix.isdigit():
            continue
        participant = str(int(suffix))
        token = mesa_subject_token(participant)
        item = manifest_subjects.get(token)
        if item is None:
            aggregate["excluded_not_in_fixed_manifest"] += 1
            continue
        if participant in demo.index:
            drow = demo.loc[participant]
            age = float(drow["nsrr_age"]) if pd.notna(drow["nsrr_age"]) else None
            sex = str(drow["nsrr_sex"]) if pd.notna(drow["nsrr_sex"]) else None
        else:
            age = None
            sex = None
            aggregate["subjects_missing_demographics"] += 1
        result, qc, actual_hash = extract_mesa_episodes(
            path, participant, item["split"], age, sex,
        )
        if actual_hash != item["source_sha256"]:
            raise ValueError(f"MESA fixed source hash changed: {path.name}")
        outputs.append(result)
        sources.append(source_record(path, actual_hash))
        aggregate.update(qc)
        aggregate["processed_subjects"] += 1
    if aggregate["processed_subjects"] != len(manifest_subjects):
        raise ValueError("not every fixed MESA manifest subject was found")
    combined = pd.concat(outputs, ignore_index=True) if outputs else pd.DataFrame(columns=OUTPUT_COLUMNS)
    aggregate["output_rows"] = int(len(combined))
    aggregate["output_subjects"] = int(combined["subject_id"].nunique()) if len(combined) else 0
    return combined, dict(sorted(aggregate.items())), sources


def _serialize_timestamps(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in ("cutoff", "bed", "wake", "observed_at"):
        result[column] = result[column].map(lambda value: pd.Timestamp(value).isoformat())
    return result


def build_all(workspace: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    output.mkdir(parents=True)
    cmi_events_path = workspace / "data" / "데이터셋" / "CMI Detect Sleep States" / "train_events.csv"
    cmi_split_path = workspace / "model" / "데이터모델" / "연구용데이터" / "cmi_run_20261010_v2" / "split_manifest_private.json"
    samsung_path = workspace / "data" / "보조데이터셋" / "개인기록_과거작업" / "260925" / "한서윤데이터_전처리" / "output" / "sleep_episodes.parquet"
    samsung_reference_path = workspace / "model" / "데이터모델" / "연구용데이터" / "tabular_run_20261010" / "personal" / "model_data.parquet"
    mesa_manifest_path = workspace / "model" / "데이터모델" / "연구용데이터" / "mesa_run_20261010" / "manifest.json"
    mesa_demo_path = workspace / "data" / "데이터셋" / "MESA" / "datasets" / "mesa-sleep-harmonized-dataset-0.8.0.csv"
    mesa_actigraphy = workspace / "data" / "데이터셋" / "MESA" / "actigraphy"

    splits = json.loads(cmi_split_path.read_text(encoding="utf-8"))
    cmi_events = pd.read_csv(cmi_events_path, usecols=["series_id", "night", "event", "timestamp"])
    cmi, cmi_qc = build_cmi_episodes(cmi_events, splits)
    samsung, samsung_qc = build_samsung_episodes(
        pd.read_parquet(samsung_path), pd.read_parquet(samsung_reference_path),
    )
    mesa_manifest = json.loads(mesa_manifest_path.read_text(encoding="utf-8"))
    demographics = pd.read_csv(mesa_demo_path, usecols=["nsrrid", "nsrr_age", "nsrr_sex"])
    mesa, mesa_qc, mesa_sources = build_mesa_episodes(mesa_actigraphy, mesa_manifest, demographics)
    episodes = pd.concat([cmi, samsung, mesa], ignore_index=True)
    episodes = episodes.sort_values(["cohort", "subject_id", "cutoff"]).reset_index(drop=True)
    episodes.to_parquet(output / "episodes.parquet", index=False)
    _serialize_timestamps(episodes).to_csv(output / "episodes.csv", index=False)

    metadata = {
        "schema_version": 1,
        "time_contract": {
            "cutoff": "20:00 local wall clock preceding the target night",
            "serialization_offset": "+09:00 fixed representation",
            "location_caveat": "The fixed offset preserves wall-clock fields and does not assert Korean location for CMI or MESA.",
            "observed_at": "wake time; offline target-availability assumption",
        },
        "columns": OUTPUT_COLUMNS,
        "cohorts": {
            "CMI": {"qc": cmi_qc, "label_source": "exact same-night onset/wakeup event pair; not PSG stages"},
            "Samsung": {"qc": samsung_qc, "label_source": "main, QC-passing consumer-device episode boundary"},
            "MESA": {
                "qc": mesa_qc,
                "label_source": "ACT wake=0 within continuous on-wrist REST-S onset/offset spans; weak ACT-derived label, not PSG",
                "date_caveat": "date_shifted preserves weekday on a reconstructed year-2000 calendar and is not an actual season/date",
            },
        },
        "sources": [
            source_record(cmi_events_path), source_record(cmi_split_path), source_record(samsung_path),
            source_record(samsung_reference_path), source_record(mesa_manifest_path), source_record(mesa_demo_path),
            *mesa_sources,
        ],
        "outputs": {
            "episodes.parquet": {"rows": int(len(episodes)), "sha256": sha256_file(output / "episodes.parquet")},
            "episodes.csv": {"rows": int(len(episodes)), "sha256": sha256_file(output / "episodes.csv")},
        },
    }
    (output / "dataset_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8",
    )
    return metadata


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> None:
    args = parse_args(argv)
    metadata = build_all(args.workspace.resolve(), args.output.resolve())
    print(json.dumps({
        "output": str(args.output.resolve()),
        "rows": metadata["outputs"]["episodes.parquet"]["rows"],
        "cohorts": {key: value["qc"].get("output_rows") for key, value in metadata["cohorts"].items()},
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
