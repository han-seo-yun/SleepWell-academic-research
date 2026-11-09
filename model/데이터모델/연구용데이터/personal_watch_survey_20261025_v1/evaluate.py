#!/usr/bin/env python3
"""Reproducible, read-only personal Samsung sleep-boundary evaluation.

The target is the consumer-device main sleep interval following a local 20:00
forecast decision.  It is not PSG total sleep time (TST), sleep opportunity,
or an estimate of an individual's optimal sleep need.  The CLI never modifies
raw data, never promotes a model, and creates results only when none of the
registered result paths already exists.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import math
import os
import platform
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
SAMSUNG_START = "com.samsung.health.sleep.start_time"
SAMSUNG_END = "com.samsung.health.sleep.end_time"
SAMSUNG_OFFSET = "com.samsung.health.sleep.time_offset"
SAMSUNG_UUID = "com.samsung.health.sleep.datauuid"
SAMSUNG_UPDATE = "com.samsung.health.sleep.update_time"
EXPORT_RE = re.compile(r"^com\.samsung\.shealth\.sleep\.(\d+)\.csv$")
OFFSET_RE = re.compile(r"^UTC([+-])(\d{2}):?(\d{2})$")

# Identity is pinned to export identifiers, never inferred from a person's name
# or a parent folder.  Unknown exports fail closed rather than being merged.
PARTICIPANT_EXPORT_IDS = {
    "P01": frozenset({"20260919081592"}),
    "P02": frozenset({"20260915142938", "20261025094402"}),
    "P03": frozenset({"20261025110878"}),
}
EXPECTED_P01_SPLITS = {"train": 137, "validation": 29, "test": 30}
RESULT_FILENAMES = (
    "predictions.csv",
    "constants_history.csv",
    "metrics.json",
    "data_audit.json",
    "provenance.json",
)
ROBUST_MODEL_FILES = {
    "cmi": "robust_cmi_v2.json",
    "mesa": "robust_mesa_v2.json",
}
ARM_METADATA = {
    "baseline_last_night": {
        "family": "causal_baseline",
        "description": "Most recent completed eligible watch interval; train-only prior fallback.",
    },
    "baseline_trailing_7_median": {
        "family": "causal_baseline",
        "description": "Median of the last seven completed eligible watch intervals; train-only prior fallback.",
    },
    "legacy_physics_fixed_constants": {
        "family": "legacy_physics",
        "description": "night_core Two-Process observation model with fixed train prior and fixed physiological constants.",
    },
    "legacy_physics_personal_constants_n14": {
        "family": "legacy_physics",
        "description": "night_core legacy n>=14 behavioral schedule gate; not current app daily personalization.",
    },
    "frozen_cmi_data_v2": {
        "family": "frozen_portable_research_model",
        "description": "Actual exported CMI data-arm inference under the robust-v2 feature contract.",
    },
    "frozen_cmi_hybrid_v2": {
        "family": "frozen_portable_research_model",
        "description": "Actual exported CMI hybrid-arm inference under the robust-v2 feature contract.",
    },
    "frozen_mesa_data_v2": {
        "family": "frozen_portable_research_model",
        "description": "Actual exported MESA data-arm inference under the robust-v2 feature contract.",
    },
    "frozen_mesa_hybrid_v2": {
        "family": "frozen_portable_research_model",
        "description": "Actual exported MESA hybrid-arm inference under the robust-v2 feature contract.",
    },
}


@dataclass(frozen=True)
class SamsungSession:
    participant_id: str
    record_id: str
    start: datetime
    end: datetime
    reported_sleep_min: float | None
    updated_at: datetime | None
    time_offset_min: int
    source_paths: tuple[str, ...]
    export_ids: tuple[str, ...]
    raw_record_ids: tuple[str, ...]


@dataclass(frozen=True)
class Episode:
    participant_id: str
    start: datetime
    end: datetime
    cutoff: datetime
    reported_sleep_min: float | None
    time_offset_min: int
    session_count: int
    source_paths: tuple[str, ...]
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class TargetNight:
    participant_id: str
    target_id: str
    split: str
    cutoff: datetime
    bed: datetime
    wake: datetime
    observed_at: datetime
    source_paths: tuple[str, ...]
    record_hashes: tuple[str, ...]
    provenance_status: str


@dataclass(frozen=True)
class SurveyRecord:
    participant_id: str
    study_date: date
    saved_at: datetime
    values: dict[str, str]
    source_path: str
    row_number: int


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: str, length: int = 20) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def relative_path(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def parse_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_time_offset(value: str) -> tuple[timezone, int]:
    match = OFFSET_RE.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"Unsupported Samsung time_offset: {value!r}")
    sign = 1 if match.group(1) == "+" else -1
    minutes = sign * (int(match.group(2)) * 60 + int(match.group(3)))
    if not -14 * 60 <= minutes <= 14 * 60:
        raise ValueError(f"Out-of-range Samsung time_offset: {value!r}")
    return timezone(timedelta(minutes=minutes)), minutes


def parse_samsung_time(value: str, time_offset: str) -> datetime:
    """Parse Samsung sleep clocks using their inspected UTC-clock contract.

    Samsung sleep start/end strings in the supported export schemas are naive
    UTC clock values.  ``time_offset`` specifies the wall-clock offset to which
    that instant is converted.  A naive timestamp is therefore never treated as
    local time.  Explicitly offset timestamps are honored as instants.
    """
    zone, _ = parse_time_offset(time_offset)
    text = str(value).strip()
    if not text:
        raise ValueError("Missing Samsung timestamp")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(zone)


def parse_saved_at(value: str) -> datetime:
    text = str(value).strip()
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Survey savedAt must carry an explicit offset")
    return parsed.astimezone(UTC)


def decision_cutoff(sleep_start: datetime) -> datetime:
    if sleep_start.tzinfo is None:
        raise ValueError("Sleep start must be timezone-aware")
    cutoff = sleep_start.replace(hour=20, minute=0, second=0, microsecond=0)
    return cutoff if sleep_start >= cutoff else cutoff - timedelta(days=1)


def offset_minutes(value: datetime, cutoff: datetime) -> float:
    return (value - cutoff).total_seconds() / 60.0


def export_id_for_path(path: Path) -> str:
    match = EXPORT_RE.fullmatch(path.name)
    if not match:
        raise ValueError(f"Not a Samsung sleep CSV: {path}")
    return match.group(1)


def participant_for_export(export_id: str) -> str:
    matches = [pid for pid, identifiers in PARTICIPANT_EXPORT_IDS.items() if export_id in identifiers]
    if len(matches) != 1:
        raise ValueError(
            f"Samsung export {export_id!r} has {len(matches)} pinned participant mappings; "
            "refusing name/folder inference"
        )
    return matches[0]


def discover_sleep_files(personal_root: Path) -> list[tuple[Path, str, str]]:
    discovered: list[tuple[Path, str, str]] = []
    for path in sorted(personal_root.rglob("com.samsung.shealth.sleep.*.csv")):
        if not path.is_file() or not EXPORT_RE.fullmatch(path.name):
            continue
        export_id = export_id_for_path(path)
        discovered.append((path, participant_for_export(export_id), export_id))
    if not discovered:
        raise FileNotFoundError(f"No Samsung sleep CSVs under {personal_root}")
    return discovered


def read_samsung_sleep_csv(
    path: Path, participant_id: str, export_id: str, workspace: Path
) -> tuple[list[SamsungSession], dict[str, Any]]:
    source = relative_path(path, workspace)
    sessions: list[SamsungSession] = []
    reasons: Counter[str] = Counter()
    offsets: Counter[str] = Counter()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        metadata_line = handle.readline()
        metadata = next(csv.reader([metadata_line])) if metadata_line else []
        header_line = handle.readline()
        headers = next(csv.reader([header_line])) if header_line else []
        if not metadata or metadata[0] != "com.samsung.shealth.sleep":
            raise ValueError(f"Unexpected Samsung dataset metadata in {source}")
        required = {SAMSUNG_START, SAMSUNG_END, SAMSUNG_OFFSET, SAMSUNG_UUID, "sleep_duration"}
        missing = required - set(headers)
        if missing:
            raise ValueError(f"Samsung sleep schema missing {sorted(missing)} in {source}")
        reader = csv.DictReader(handle, fieldnames=headers)
        for row_number, row in enumerate(reader, start=3):
            reasons["raw_rows"] += 1
            raw_offset = (row.get(SAMSUNG_OFFSET) or "").strip()
            try:
                _, offset_value = parse_time_offset(raw_offset)
                offsets[raw_offset] += 1
                start = parse_samsung_time(row.get(SAMSUNG_START) or "", raw_offset)
                end = parse_samsung_time(row.get(SAMSUNG_END) or "", raw_offset)
            except (TypeError, ValueError, OverflowError):
                reasons["invalid_or_missing_time_or_offset"] += 1
                continue
            updated_at = None
            raw_update = (row.get(SAMSUNG_UPDATE) or "").strip()
            if raw_update:
                try:
                    updated_at = parse_samsung_time(raw_update, raw_offset)
                except (TypeError, ValueError, OverflowError):
                    reasons["invalid_update_time"] += 1
            reported = parse_float(row.get("sleep_duration"))
            raw_uuid = (row.get(SAMSUNG_UUID) or "").strip()
            fallback = stable_hash(
                f"{participant_id}|{start.astimezone(UTC).isoformat()}|"
                f"{end.astimezone(UTC).isoformat()}|{reported}"
            )
            record_id = raw_uuid or f"fingerprint:{fallback}"
            sessions.append(
                SamsungSession(
                    participant_id=participant_id,
                    record_id=record_id,
                    start=start,
                    end=end,
                    reported_sleep_min=reported,
                    updated_at=updated_at,
                    time_offset_min=offset_value,
                    source_paths=(source,),
                    export_ids=(export_id,),
                    raw_record_ids=(record_id,),
                )
            )
            reasons["parsed_rows"] += 1
    audit = {
        "path": source,
        "participant_id": participant_id,
        "export_id": export_id,
        "dataset": metadata[0],
        "export_app_version": metadata[1] if len(metadata) > 1 else None,
        "schema_version": metadata[2] if len(metadata) > 2 else None,
        "timestamp_columns": [SAMSUNG_START, SAMSUNG_END, SAMSUNG_UPDATE],
        "timestamp_semantics": (
            "Supported Samsung sleep schema stores naive start/end/update strings as UTC clocks; "
            "each instant is converted with that row's explicit time_offset."
        ),
        "observed_time_offsets": dict(sorted(offsets.items())),
        "counts": dict(sorted(reasons.items())),
    }
    return sessions, audit


def _choice_key(session: SamsungSession) -> tuple[float, float, str]:
    updated = session.updated_at.timestamp() if session.updated_at is not None else float("-inf")
    return updated, session.end.timestamp(), "|".join(session.source_paths)


def _merge_session_group(group: Sequence[SamsungSession]) -> SamsungSession:
    chosen = max(group, key=_choice_key)
    return replace(
        chosen,
        source_paths=tuple(sorted({item for row in group for item in row.source_paths})),
        export_ids=tuple(sorted({item for row in group for item in row.export_ids})),
        raw_record_ids=tuple(sorted({item for row in group for item in row.raw_record_ids})),
    )


def deduplicate_sessions(
    sessions: Sequence[SamsungSession],
) -> tuple[list[SamsungSession], dict[str, int]]:
    """Deduplicate within participant and union every contributing source path."""
    primary: dict[tuple[str, str], list[SamsungSession]] = defaultdict(list)
    for row in sessions:
        primary[(row.participant_id, row.record_id)].append(row)
    conflicts = sum(
        len({(r.start.astimezone(UTC), r.end.astimezone(UTC)) for r in group}) > 1
        for group in primary.values()
    )
    first_pass = [_merge_session_group(group) for group in primary.values()]

    semantic: dict[tuple[str, str, str], list[SamsungSession]] = defaultdict(list)
    for row in first_pass:
        semantic[
            (
                row.participant_id,
                row.start.astimezone(UTC).isoformat(),
                row.end.astimezone(UTC).isoformat(),
            )
        ].append(row)
    result = [_merge_session_group(group) for group in semantic.values()]
    result.sort(key=lambda row: (row.participant_id, row.start, row.end, row.record_id))
    return result, {
        "input_rows": len(sessions),
        "uuid_or_fingerprint_groups": len(primary),
        "conflicting_record_id_groups": conflicts,
        "semantic_interval_groups": len(semantic),
        "duplicates_removed": len(sessions) - len(result),
        "output_rows": len(result),
    }


def build_eligible_episodes(
    sessions: Sequence[SamsungSession], participant_id: str
) -> tuple[list[Episode], dict[str, int]]:
    rows = sorted(
        [row for row in sessions if row.participant_id == participant_id],
        key=lambda row: (row.start, row.end, row.record_id),
    )
    counts: Counter[str] = Counter(source_sessions=len(rows))
    groups: list[list[SamsungSession]] = []
    for row in rows:
        if row.end <= row.start:
            counts["nonpositive_sessions"] += 1
            continue
        if (
            not groups
            or row.time_offset_min != groups[-1][0].time_offset_min
            or row.start > max(item.end for item in groups[-1]) + timedelta(hours=2)
        ):
            groups.append([row])
        else:
            groups[-1].append(row)
    counts["merged_episode_groups"] = len(groups)

    candidates: list[Episode] = []
    for group in groups:
        start = min(row.start for row in group)
        end = max(row.end for row in group)
        reported_values = [row.reported_sleep_min for row in group if row.reported_sleep_min is not None]
        reported = sum(reported_values) if reported_values else None
        interval = (end - start).total_seconds() / 60.0
        if not (30 <= interval <= 960 and reported is not None and 20 <= reported <= 840):
            counts["episode_qc_excluded"] += 1
            continue
        candidates.append(
            Episode(
                participant_id=participant_id,
                start=start,
                end=end,
                cutoff=decision_cutoff(start),
                reported_sleep_min=reported,
                time_offset_min=group[0].time_offset_min,
                session_count=len(group),
                source_paths=tuple(sorted({item for row in group for item in row.source_paths})),
                record_ids=tuple(sorted({item for row in group for item in row.raw_record_ids})),
            )
        )
    counts["episode_qc_pass"] = len(candidates)

    # Preserve the preprocessing convention: largest reported sleep per local
    # wake date is the main episode.  Ties are deterministic.
    by_wake_date: dict[date, list[Episode]] = defaultdict(list)
    for episode in candidates:
        by_wake_date[episode.end.date()].append(episode)
    main: list[Episode] = []
    for group in by_wake_date.values():
        main.append(
            max(
                group,
                key=lambda row: (
                    row.reported_sleep_min or float("-inf"),
                    (row.end - row.start).total_seconds(),
                    -row.start.timestamp(),
                ),
            )
        )
    counts["non_main_excluded"] = len(candidates) - len(main)

    common: list[Episode] = []
    for episode in main:
        bed_offset = offset_minutes(episode.start, episode.cutoff)
        wake_offset = offset_minutes(episode.end, episode.cutoff)
        duration = wake_offset - bed_offset
        if episode.time_offset_min != 540:
            counts["unsupported_non_kst_model_contract"] += 1
        elif not (0 <= bed_offset <= 960 and wake_offset <= 1440 and 30 <= duration <= 960):
            counts["outside_common_target_window"] += 1
        else:
            common.append(episode)

    # One target per local 20:00 decision, retaining the longest interval.
    by_cutoff: dict[datetime, list[Episode]] = defaultdict(list)
    for episode in common:
        by_cutoff[episode.cutoff].append(episode)
    eligible = [
        max(group, key=lambda row: ((row.end - row.start).total_seconds(), -row.start.timestamp()))
        for group in by_cutoff.values()
    ]
    eligible.sort(key=lambda row: row.cutoff)
    counts["duplicate_cutoff_excluded"] = len(common) - len(eligible)
    counts["eligible_targets"] = len(eligible)
    return eligible, dict(sorted(counts.items()))


def chronological_splits(count: int) -> list[str]:
    """Predeclared new-participant 60/20/20 chronological allocation."""
    if count < 0:
        raise ValueError("count must be nonnegative")
    train_end = math.ceil(count * 0.60)
    validation_end = math.ceil(count * 0.80)
    return [
        "train" if index < train_end else "validation" if index < validation_end else "test"
        for index in range(count)
    ]


def targets_from_episodes(episodes: Sequence[Episode]) -> list[TargetNight]:
    ordered = sorted(episodes, key=lambda row: row.cutoff)
    result: list[TargetNight] = []
    for episode, split in zip(ordered, chronological_splits(len(ordered))):
        identity = stable_hash(
            f"{episode.participant_id}|{episode.cutoff.isoformat()}|"
            f"{episode.start.isoformat()}|{episode.end.isoformat()}"
        )
        result.append(
            TargetNight(
                participant_id=episode.participant_id,
                target_id=f"{episode.participant_id}_{identity}",
                split=split,
                cutoff=episode.cutoff,
                bed=episode.start,
                wake=episode.end,
                observed_at=episode.end,
                source_paths=episode.source_paths,
                record_hashes=tuple(stable_hash(value) for value in episode.record_ids),
                provenance_status="raw_deduplicated_episode",
            )
        )
    return result


def load_frozen_p01_targets(
    frozen_csv: Path, raw_episodes: Sequence[Episode], workspace: Path
) -> tuple[list[TargetNight], dict[str, Any]]:
    rows: list[dict[str, str]] = []
    with frozen_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("cohort", "").lower() == "samsung":
                rows.append(row)
    if len(rows) != 196:
        raise ValueError(f"Frozen P01 next-night dataset must contain 196 Samsung rows, found {len(rows)}")
    split_counts = Counter(row["split"] for row in rows)
    if dict(split_counts) != EXPECTED_P01_SPLITS:
        raise ValueError(f"Frozen P01 split changed: {dict(split_counts)}")
    if len({row["subject_id"] for row in rows}) != 1:
        raise ValueError("Frozen P01 dataset contains multiple subject identifiers")

    episodes_by_cutoff: dict[datetime, list[Episode]] = defaultdict(list)
    all_sources = tuple(sorted({path for episode in raw_episodes for path in episode.source_paths}))
    for episode in raw_episodes:
        episodes_by_cutoff[episode.cutoff].append(episode)
    matched = 0
    targets: list[TargetNight] = []
    for row in sorted(rows, key=lambda item: item["cutoff"]):
        cutoff = datetime.fromisoformat(row["cutoff"])
        bed = datetime.fromisoformat(row["bed"])
        wake = datetime.fromisoformat(row["wake"])
        if any(value.utcoffset() != timedelta(hours=9) for value in (cutoff, bed, wake)):
            raise ValueError("Frozen P01 target does not satisfy the +09:00 feature contract")
        candidates = episodes_by_cutoff.get(cutoff, [])
        best = None
        if candidates:
            best = min(
                candidates,
                key=lambda episode: abs((episode.start - bed).total_seconds())
                + abs((episode.end - wake).total_seconds()),
            )
            if max(abs((best.start - bed).total_seconds()), abs((best.end - wake).total_seconds())) > 120:
                best = None
        if best is not None:
            matched += 1
            sources = best.source_paths
            hashes = tuple(stable_hash(value) for value in best.record_ids)
            status = "frozen_196_target_matched_to_raw"
        else:
            sources = all_sources
            hashes = ()
            status = "frozen_196_target_participant_level_raw_provenance"
        identity = stable_hash(f"P01|{cutoff.isoformat()}|{bed.isoformat()}|{wake.isoformat()}")
        targets.append(
            TargetNight(
                participant_id="P01",
                target_id=f"P01_{identity}",
                split=row["split"],
                cutoff=cutoff,
                bed=bed,
                wake=wake,
                observed_at=datetime.fromisoformat(row["observed_at"]),
                source_paths=sources,
                record_hashes=hashes,
                provenance_status=status,
            )
        )
    return targets, {
        "path": relative_path(frozen_csv, workspace),
        "rows": len(targets),
        "split_counts": dict(sorted(split_counts.items())),
        "raw_boundary_matches_within_2min": matched,
        "broader_preprocessed_main_qc_rows_not_substituted": 219,
        "preserved_contract": "existing common-window 196-night next-night split",
    }


def survey_participant_for_filename(name: str) -> str:
    if name == "sleep-study-checkins-2.csv":
        return "P03"
    if name.startswith("sleep-study-checkins_") and name.endswith(".csv"):
        return "P04"
    raise ValueError(f"Unmapped check-in filename {name!r}; refusing name-column identity inference")


def discover_and_read_surveys(
    personal_root: Path, workspace: Path
) -> tuple[list[SurveyRecord], list[dict[str, Any]], list[Path]]:
    surveys: list[SurveyRecord] = []
    audits: list[dict[str, Any]] = []
    paths = sorted(path for path in personal_root.rglob("sleep-study-checkins*.csv") if path.is_file())
    for path in paths:
        participant_id = survey_participant_for_filename(path.name)
        source = relative_path(path, workspace)
        rows = 0
        invalid = 0
        dates: list[date] = []
        saved_local: list[datetime] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = reader.fieldnames or []
            required = {"date", "savedAt"}
            if not required.issubset(headers):
                raise ValueError(f"Survey schema missing {sorted(required - set(headers))} in {source}")
            for row_number, row in enumerate(reader, start=2):
                rows += 1
                try:
                    study_date = date.fromisoformat((row.get("date") or "").strip())
                    saved_at = parse_saved_at(row.get("savedAt") or "")
                except (TypeError, ValueError, OverflowError):
                    invalid += 1
                    continue
                values = {key: (value or "").strip() for key, value in row.items() if key != "participant"}
                surveys.append(
                    SurveyRecord(participant_id, study_date, saved_at, values, source, row_number)
                )
                dates.append(study_date)
                saved_local.append(saved_at.astimezone(KST))
        audits.append(
            {
                "path": source,
                "participant_id": participant_id,
                "rows": rows,
                "valid_rows": rows - invalid,
                "invalid_rows": invalid,
                "date_min": min(dates).isoformat() if dates else None,
                "date_max": max(dates).isoformat() if dates else None,
                "saved_at_local_min": min(saved_local).isoformat() if saved_local else None,
                "saved_at_local_max": max(saved_local).isoformat() if saved_local else None,
                "schema_has_rested": "rested" in headers,
                "participant_name_column_used_for_identity": False,
            }
        )
    return surveys, audits, paths


def survey_at_cutoff(
    records: Sequence[SurveyRecord], participant_id: str, cutoff: datetime, *, available_only: bool
) -> SurveyRecord | None:
    candidates = [
        row
        for row in records
        if row.participant_id == participant_id
        and row.study_date == cutoff.date()
        and (not available_only or row.saved_at < cutoff.astimezone(UTC))
    ]
    return max(candidates, key=lambda row: (row.saved_at, row.source_path, row.row_number)) if candidates else None


def eligible_history(records: Sequence[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    """Return only completed records strictly observed before a decision."""
    output: list[dict[str, Any]] = []
    for record in records:
        try:
            wake = datetime.fromisoformat(str(record["wake"]))
            observed = datetime.fromisoformat(str(record["observed_at"]))
        except (KeyError, TypeError, ValueError):
            continue
        if wake < cutoff and observed < cutoff:
            output.append(record)
    return sorted(output, key=lambda row: (row["wake"], row["observed_at"]))


def load_core_modules(workspace: Path):
    web_app = workspace / "기타" / "SleepWell-integrated" / "web-app"
    if not web_app.is_dir():
        raise FileNotFoundError(web_app)
    web_text = str(web_app)
    if web_text not in sys.path:
        sys.path.insert(0, web_text)
    night_core = importlib.import_module("night_core")
    robust_features = importlib.import_module("robust_features")
    if night_core.CONTRACT != "sleepwell_next_night_pre20_v1":
        raise ValueError(f"Unexpected night_core contract: {night_core.CONTRACT}")
    if robust_features.FEATURE_CONTRACT != "sleepwell_next_night_robust_v2":
        raise ValueError(f"Unexpected robust feature contract: {robust_features.FEATURE_CONTRACT}")
    return night_core, robust_features


def load_robust_models(workspace: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Path]]:
    model_dir = workspace / "기타" / "SleepWell-integrated" / "web-app" / "models"
    models: dict[str, dict[str, Any]] = {}
    paths: dict[str, Path] = {}
    for cohort, filename in ROBUST_MODEL_FILES.items():
        path = model_dir / filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 2:
            raise ValueError(f"Unsupported portable model schema in {path}")
        if payload.get("artifact_type") != "sleepwell_robust_next_night_portable":
            raise ValueError(f"Unexpected robust artifact type in {path}")
        if payload.get("feature_contract") != "sleepwell_next_night_robust_v2":
            raise ValueError(f"Unexpected robust feature contract in {path}")
        if payload.get("cohort") != cohort or set(payload.get("models", {})) != {"data", "hybrid"}:
            raise ValueError(f"Unexpected robust model arms/cohort in {path}")
        models[cohort] = payload
        paths[cohort] = path
    return models, paths


def _tree_predict(tree: Sequence[dict[str, Any]], row: Sequence[float]) -> float:
    index = 0
    while "leaf" not in tree[index]:
        node = tree[index]
        value = row[int(node["feature"])]
        go_left = (math.isnan(value) and bool(node["missing_left"])) or (
            not math.isnan(value) and value <= float(node["threshold"])
        )
        index = int(node["left"] if go_left else node["right"])
    return float(tree[index]["leaf"])


def portable_predict_one(spec: dict[str, Any], source: Sequence[float]) -> tuple[float, float]:
    """Exact scalar counterpart of tools/model/export_robust_portable.py."""
    prep = spec["preprocessing"]
    if len(source) != len(spec["feature_order"]):
        raise ValueError("Portable model feature dimension mismatch")
    missing = [math.isnan(float(value)) for value in source]
    row = [
        float(prep["imputer_median"][index]) if missing[index] else float(value)
        for index, value in enumerate(source)
    ]
    row.extend(1.0 if missing[index] else 0.0 for index in prep["missing_indicator_features"])
    row = [
        min(float(prep["clip_high"][index]), max(float(prep["clip_low"][index]), value))
        for index, value in enumerate(row)
    ]
    row = [row[int(index)] for index in prep["variance_keep"]]
    row = [
        (value - float(prep["scale_mean"][index])) / float(prep["scale"][index])
        for index, value in enumerate(row)
    ]
    predicted: list[float] = []
    for estimator in spec["estimators"]:
        if estimator["kind"] == "linear":
            value = float(estimator["intercept"]) + sum(
                float(coefficient) * feature
                for coefficient, feature in zip(estimator["coefficients"], row)
            )
        elif estimator["kind"] == "hist_gradient_boosting":
            value = float(estimator["baseline"]) + sum(
                _tree_predict(tree, row) for tree in estimator["trees"]
            )
        else:
            raise ValueError(f"Unsupported portable estimator {estimator['kind']!r}")
        predicted.append(value)
    if len(predicted) != 2 or not all(math.isfinite(value) for value in predicted):
        raise ValueError("Portable model did not produce two finite predictions")
    return predicted[0], predicted[1]


def participant_train_prior(targets: Sequence[TargetNight]) -> dict[str, float]:
    train = [target for target in targets if target.split == "train"]
    if not train:
        raise ValueError(f"{targets[0].participant_id if targets else 'participant'} has no train targets")
    return {
        "bed_offset": statistics.mean(offset_minutes(target.bed, target.cutoff) for target in train),
        "wake_offset": statistics.mean(offset_minutes(target.wake, target.cutoff) for target in train),
    }


def _prediction_row(
    target: TargetNight,
    arm: str,
    predicted_bed: float,
    predicted_wake: float,
    history: Sequence[TargetNight],
    prior: dict[str, float],
    model_status: str,
) -> dict[str, Any]:
    actual_bed = offset_minutes(target.bed, target.cutoff)
    actual_wake = offset_minutes(target.wake, target.cutoff)
    predicted_duration = predicted_wake - predicted_bed
    actual_duration = actual_wake - actual_bed
    onset_error = abs(predicted_bed - actual_bed)
    wake_error = abs(predicted_wake - actual_wake)
    return {
        "participant_id": target.participant_id,
        "target_id": target.target_id,
        "split": target.split,
        "cutoff": target.cutoff.isoformat(),
        "sleep_start": target.bed.isoformat(),
        "sleep_end": target.wake.isoformat(),
        "observed_at": target.observed_at.isoformat(),
        "arm": arm,
        "predicted_onset_offset_min": predicted_bed,
        "predicted_wake_offset_min": predicted_wake,
        "predicted_interval_min": predicted_duration,
        "actual_onset_offset_min": actual_bed,
        "actual_wake_offset_min": actual_wake,
        "actual_interval_min": actual_duration,
        "onset_abs_error_min": onset_error,
        "wake_abs_error_min": wake_error,
        "interval_abs_error_min": abs(predicted_duration - actual_duration),
        "joint_abs_error_min": (onset_error + wake_error) / 2.0,
        "history_count": len(history),
        "max_history_observed_at": max((row.observed_at for row in history), default=None).isoformat()
        if history
        else "",
        "prior_bed_offset_min": prior["bed_offset"],
        "prior_wake_offset_min": prior["wake_offset"],
        "model_status": model_status,
        "source_file_count": len(target.source_paths),
        "source_record_count": len(target.record_hashes),
        "provenance_status": target.provenance_status,
    }


def evaluate_predictions(
    all_targets: Sequence[TargetNight], night_core, robust_features, robust_models: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, float]]]:
    predictions: list[dict[str, Any]] = []
    constants: list[dict[str, Any]] = []
    priors: dict[str, dict[str, float]] = {}
    grouped: dict[str, list[TargetNight]] = defaultdict(list)
    for target in all_targets:
        grouped[target.participant_id].append(target)

    for participant_id, participant_targets in sorted(grouped.items()):
        participant_targets.sort(key=lambda row: row.cutoff)
        prior = participant_train_prior(participant_targets)
        priors[participant_id] = prior
        enrolled_at = participant_targets[0].cutoff
        core_records = [
            {
                "id": target.target_id,
                "sleep_start": target.bed.isoformat(),
                "wake": target.wake.isoformat(),
                "observed_at": target.observed_at.isoformat(),
                "source": "health_connect",
            }
            for target in participant_targets
        ]
        previous_personal_state: dict[str, Any] | None = None
        for target in participant_targets:
            history = [
                row
                for row in participant_targets
                if row.wake < target.cutoff and row.observed_at < target.cutoff
            ]
            if history and max(row.observed_at for row in history) >= target.cutoff:
                raise AssertionError("Future observation entered history")
            history = sorted(history, key=lambda row: row.wake)
            historical_pairs = [
                (offset_minutes(row.bed, row.cutoff), offset_minutes(row.wake, row.cutoff))
                for row in history
            ]
            if historical_pairs:
                last_bed, last_wake = historical_pairs[-1]
                recent = historical_pairs[-7:]
                median_bed = statistics.median(value[0] for value in recent)
                median_wake = statistics.median(value[1] for value in recent)
                baseline_status = "past_only_history"
            else:
                last_bed, last_wake = prior["bed_offset"], prior["wake_offset"]
                median_bed, median_wake = last_bed, last_wake
                baseline_status = "train_prior_fallback_no_past_night"
            last_bed, last_wake = night_core.constrain(last_bed, last_wake)
            median_bed, median_wake = night_core.constrain(median_bed, median_wake)
            predictions.append(
                _prediction_row(
                    target, "baseline_last_night", last_bed, last_wake, history, prior, baseline_status
                )
            )
            predictions.append(
                _prediction_row(
                    target,
                    "baseline_trailing_7_median",
                    median_bed,
                    median_wake,
                    history,
                    prior,
                    baseline_status,
                )
            )

            fixed_values, fixed_state = night_core.features(
                core_records,
                target.cutoff,
                prior,
                personalize=False,
                enrolled_at=enrolled_at,
            )
            personal_values, personal_state = night_core.features(
                core_records,
                target.cutoff,
                prior,
                personalize=True,
                enrolled_at=enrolled_at,
            )
            fixed_bed, fixed_wake = night_core.constrain(
                fixed_values["physical_bed"], fixed_values["physical_wake"]
            )
            personal_bed, personal_wake = night_core.constrain(
                personal_values["physical_bed"], personal_values["physical_wake"]
            )
            predictions.append(
                _prediction_row(
                    target,
                    "legacy_physics_fixed_constants",
                    fixed_bed,
                    fixed_wake,
                    history,
                    prior,
                    "night_core personalize=False; fixed tau",
                )
            )
            predictions.append(
                _prediction_row(
                    target,
                    "legacy_physics_personal_constants_n14",
                    personal_bed,
                    personal_wake,
                    history,
                    prior,
                    f"night_core legacy mode={personal_state['mode']}; fixed tau",
                )
            )

            constants.append(
                {
                    "participant_id": participant_id,
                    "target_id": target.target_id,
                    "split": target.split,
                    "cutoff": target.cutoff.isoformat(),
                    "history_count": len(history),
                    "fixed_habit_bed_offset_min": fixed_state["habit_bed_offset"],
                    "fixed_habit_wake_offset_min": fixed_state["habit_wake_offset"],
                    "personal_mode": personal_state["mode"],
                    "personal_weight": personal_state["personal_weight"],
                    "personal_habit_bed_offset_min": personal_state["habit_bed_offset"],
                    "personal_habit_wake_offset_min": personal_state["habit_wake_offset"],
                    "personal_phase_min": personal_state["phase_min"],
                    "personal_sleep_threshold": personal_state["sleep_threshold"],
                    "personal_wake_threshold": personal_state["wake_threshold"],
                    "delta_personal_habit_bed_min": (
                        personal_state["habit_bed_offset"] - previous_personal_state["habit_bed_offset"]
                        if previous_personal_state is not None
                        else None
                    ),
                    "delta_personal_habit_wake_min": (
                        personal_state["habit_wake_offset"] - previous_personal_state["habit_wake_offset"]
                        if previous_personal_state is not None
                        else None
                    ),
                    "tau_wake_h": personal_state["tau_wake_h"],
                    "tau_sleep_h": personal_state["tau_sleep_h"],
                    "physiological_tau_estimated": False,
                    "interpretation": (
                        "Behavioral schedule-derived observation state; device boundaries cannot validate true physiology."
                    ),
                }
            )
            previous_personal_state = personal_state

            for cohort in ("cmi", "mesa"):
                artifact = robust_models[cohort]
                artifact_prior = artifact["prior"]
                values, _ = robust_features.extended_features(
                    core_records, target.cutoff, artifact_prior, enrolled_at
                )
                for model_arm in ("data", "hybrid"):
                    model_spec = artifact["models"][model_arm]
                    feature_row = [float(values[name]) for name in model_spec["feature_order"]]
                    predicted_bed, predicted_wake = portable_predict_one(model_spec, feature_row)
                    predicted_bed, predicted_wake = night_core.constrain(predicted_bed, predicted_wake)
                    predictions.append(
                        _prediction_row(
                            target,
                            f"frozen_{cohort}_{model_arm}_v2",
                            predicted_bed,
                            predicted_wake,
                            history,
                            prior,
                            (
                                f"actual portable export inference; cohort={cohort}; "
                                f"contract={artifact['feature_contract']}"
                            ),
                        )
                    )
    predictions.sort(key=lambda row: (row["participant_id"], row["cutoff"], row["arm"]))
    constants.sort(key=lambda row: (row["participant_id"], row["cutoff"]))
    return predictions, constants, priors


def _metric_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "status": "not_estimable"}
    return {
        "n": len(rows),
        "onset_mae_min": statistics.mean(float(row["onset_abs_error_min"]) for row in rows),
        "wake_mae_min": statistics.mean(float(row["wake_abs_error_min"]) for row in rows),
        "interval_mae_min": statistics.mean(float(row["interval_abs_error_min"]) for row in rows),
        "joint_onset_wake_mae_min": statistics.mean(float(row["joint_abs_error_min"]) for row in rows),
        "status": "descriptive_heldout_test",
    }


def summarize_metrics(
    targets: Sequence[TargetNight], predictions: Sequence[dict[str, Any]], survey_summary: dict[str, Any]
) -> dict[str, Any]:
    target_groups: dict[str, list[TargetNight]] = defaultdict(list)
    for target in targets:
        target_groups[target.participant_id].append(target)
    test_rows = [row for row in predictions if row["split"] == "test"]
    arms = sorted({row["arm"] for row in predictions})
    per_person: dict[str, Any] = {}
    for participant_id, participant_targets in sorted(target_groups.items()):
        split_counts = Counter(row.split for row in participant_targets)
        arm_metrics = {
            arm: _metric_summary(
                [row for row in test_rows if row["participant_id"] == participant_id and row["arm"] == arm]
            )
            for arm in arms
        }
        test_n = split_counts.get("test", 0)
        per_person[participant_id] = {
            "n_targets": len(participant_targets),
            "split_counts": dict(sorted(split_counts.items())),
            "test_n": test_n,
            "low_n": test_n < 30,
            "low_n_threshold": 30,
            "arms": arm_metrics,
        }

    aggregate: dict[str, Any] = {}
    for arm in arms:
        rows = [row for row in test_rows if row["arm"] == arm]
        pooled = _metric_summary(rows)
        person_values = [
            per_person[pid]["arms"][arm]
            for pid in sorted(per_person)
            if per_person[pid]["arms"][arm].get("n", 0) > 0
        ]
        aggregate[arm] = {
            "pooled_equal_night": pooled,
            "macro_equal_person": {
                "n_people": len(person_values),
                "onset_mae_min": statistics.mean(value["onset_mae_min"] for value in person_values)
                if person_values
                else None,
                "wake_mae_min": statistics.mean(value["wake_mae_min"] for value in person_values)
                if person_values
                else None,
                "interval_mae_min": statistics.mean(value["interval_mae_min"] for value in person_values)
                if person_values
                else None,
                "joint_onset_wake_mae_min": statistics.mean(
                    value["joint_onset_wake_mae_min"] for value in person_values
                )
                if person_values
                else None,
            },
        }

    reference = "baseline_trailing_7_median"
    indexed = {
        (row["participant_id"], row["target_id"], row["arm"]): row for row in test_rows
    }
    paired: dict[str, Any] = {}
    for arm in arms:
        if arm == reference:
            continue
        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for key, candidate in indexed.items():
            if key[2] != arm:
                continue
            baseline = indexed.get((key[0], key[1], reference))
            if baseline is not None:
                pairs.append((candidate, baseline))
        per_metric: dict[str, Any] = {}
        for label, field in (
            ("onset", "onset_abs_error_min"),
            ("wake", "wake_abs_error_min"),
            ("interval", "interval_abs_error_min"),
            ("joint_onset_wake", "joint_abs_error_min"),
        ):
            differences = [float(candidate[field]) - float(base[field]) for candidate, base in pairs]
            per_metric[label] = {
                "n_pairs": len(differences),
                "mean_candidate_minus_reference_error_min": statistics.mean(differences)
                if differences
                else None,
                "median_candidate_minus_reference_error_min": statistics.median(differences)
                if differences
                else None,
                "negative_favors_candidate": True,
            }
        person_differences: dict[str, Any] = {}
        for participant_id in sorted(per_person):
            selected = [pair for pair in pairs if pair[0]["participant_id"] == participant_id]
            person_differences[participant_id] = {
                "n_pairs": len(selected),
                "joint_mean_candidate_minus_reference_error_min": statistics.mean(
                    float(candidate["joint_abs_error_min"]) - float(base["joint_abs_error_min"])
                    for candidate, base in selected
                )
                if selected
                else None,
            }
        paired[f"{arm}_vs_{reference}"] = {
            "metrics": per_metric,
            "per_person": person_differences,
        }

    return {
        "schema_version": 1,
        "target": {
            "decision": "local 20:00 before the modeled night",
            "onset": "consumer-device main sleep interval start boundary",
            "wake": "consumer-device main sleep interval end boundary",
            "interval": "wake boundary minus onset boundary; not PSG TST and not sleep opportunity",
            "not_optimal_need": True,
        },
        "protocol": {
            "split": "P01 frozen 196-night split; P02/P03 first 60% train, next 20% validation, final 20% test",
            "random_split": False,
            "primary_evaluation_split": "test",
            "history": "strictly completed and observed before each 20:00 cutoff",
            "new_participant_prior": "participant train targets only",
            "model_selection_on_test": False,
        },
        "arms": ARM_METADATA,
        "per_person": per_person,
        "aggregate": aggregate,
        "paired_differences": {
            "reference_arm": reference,
            "comparisons": paired,
            "uncertainty_intervals": None,
            "uncertainty_reason": "Few participants and low per-person test n; descriptive paired differences only.",
        },
        "survey_incorporation": survey_summary,
        "daily_personalization": {
            "status": "not_evaluated",
            "reason": (
                "Production engine.parameters requires diary-primary inputs and good-day target updates require rested>=4 "
                "with fatigue<=3. The check-in schema has no rested field and wearables are auxiliary-only; no wearable "
                "record was relabeled as diary. The legacy n>=14 physics arm is not current app daily personalization."
            ),
        },
        "interpretation": {
            "prediction_not_benefit": True,
            "physiology_validated": False,
            "reason": (
                "Device interval boundaries can compare behavioral timing prediction errors but cannot validate true "
                "physiological constants, TST, optimal need, or causal benefit from recommendations."
            ),
        },
        "limitations": [
            "All watch targets are Samsung consumer-device boundaries rather than PSG ground truth.",
            "The sleep interval is not TST, sleep opportunity, or an estimate of optimal sleep need.",
            "Two-Process tau values remain fixed engineering constants; boundary replay does not identify physiology.",
            "CMI/MESA portable models are frozen reference-cohort research models, not validated personal deployment models.",
            "Survey rows unavailable before local 20:00 are excluded from predictive incorporation; retrospective joins are association-only.",
            "P02/P03 and especially survey-linked heldout samples are small; metrics are descriptive.",
            "No model or policy is promoted to a product by this evaluation.",
        ],
    }


def _pearson(pairs: Sequence[tuple[float, float]]) -> dict[str, Any]:
    if len(pairs) < 5:
        return {"n": len(pairs), "r": None, "status": "not_estimable_fewer_than_5_pairs"}
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    x_mean, y_mean = statistics.mean(xs), statistics.mean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    x_ss = sum((x - x_mean) ** 2 for x in xs)
    y_ss = sum((y - y_mean) ** 2 for y in ys)
    if x_ss <= 0 or y_ss <= 0:
        return {"n": len(pairs), "r": None, "status": "not_estimable_zero_variance"}
    return {
        "n": len(pairs),
        "r": numerator / math.sqrt(x_ss * y_ss),
        "status": "retrospective_descriptive_association_only",
    }


def build_survey_summary(
    targets: Sequence[TargetNight], surveys: Sequence[SurveyRecord], survey_file_audits: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    versions: dict[tuple[str, date], list[SurveyRecord]] = defaultdict(list)
    for survey in surveys:
        versions[(survey.participant_id, survey.study_date)].append(survey)
    latest = [max(group, key=lambda row: (row.saved_at, row.source_path, row.row_number)) for group in versions.values()]
    participant_rows: dict[str, Any] = {}
    target_by_participant: dict[str, list[TargetNight]] = defaultdict(list)
    for target in targets:
        target_by_participant[target.participant_id].append(target)

    fatigue_pairs: list[tuple[float, float]] = []
    caffeine_pairs: list[tuple[float, float]] = []
    schedule_pairs: list[tuple[float, float]] = []
    total_strict_joins = 0
    total_retrospective_joins = 0
    total_strict_rows = 0
    for participant_id in sorted({row.participant_id for row in latest} | set(target_by_participant)):
        participant_surveys = [row for row in latest if row.participant_id == participant_id]
        on_time = [
            row
            for row in participant_surveys
            if row.saved_at < datetime.combine(row.study_date, time(20, 0), KST).astimezone(UTC)
        ]
        strict_joins = 0
        retrospective_joins = 0
        for target in target_by_participant.get(participant_id, []):
            retrospective = survey_at_cutoff(surveys, participant_id, target.cutoff, available_only=False)
            strict = survey_at_cutoff(surveys, participant_id, target.cutoff, available_only=True)
            if retrospective is not None:
                retrospective_joins += 1
                duration = offset_minutes(target.wake, target.cutoff) - offset_minutes(target.bed, target.cutoff)
                fatigue = parse_float(retrospective.values.get("fatigue"))
                caffeine = parse_float(retrospective.values.get("caffeineMg"))
                alarm = retrospective.values.get("alarmTime", "")
                commitment = retrospective.values.get("firstCommitment", "")
                if fatigue is not None:
                    fatigue_pairs.append((fatigue, duration))
                if caffeine is not None:
                    caffeine_pairs.append((caffeine, duration))
                clock_values: list[int] = []
                for raw in (alarm, commitment):
                    try:
                        parsed = time.fromisoformat(raw)
                        clock_values.append(parsed.hour * 60 + parsed.minute)
                    except ValueError:
                        pass
                if clock_values:
                    earliest_offset = min((clock - 20 * 60) % 1440 for clock in clock_values)
                    schedule_pairs.append((float(earliest_offset), offset_minutes(target.wake, target.cutoff)))
            if strict is not None:
                strict_joins += 1
        total_strict_rows += len(on_time)
        total_strict_joins += strict_joins
        total_retrospective_joins += retrospective_joins
        participant_rows[participant_id] = {
            "unique_dated_rows": len(participant_surveys),
            "strictly_available_before_20": len(on_time),
            "unavailable_or_exactly_at_20": len(participant_surveys) - len(on_time),
            "watch_targets": len(target_by_participant.get(participant_id, [])),
            "strict_watch_joins": strict_joins,
            "retrospective_watch_joins": retrospective_joins,
        }

    no_eligible = total_strict_joins == 0
    return {
        "survey_files": list(survey_file_audits),
        "per_person": participant_rows,
        "strict_availability_rule": "savedAt instant must be strictly earlier than that local study date's 20:00 cutoff",
        "strictly_available_rows": total_strict_rows,
        "strict_watch_join_rows": total_strict_joins,
        "retrospective_watch_join_rows": total_retrospective_joins,
        "predictive_survey_model": {
            "status": "not_estimable" if no_eligible else "not_fit_by_prespecified_zero-row protocol",
            "reason": (
                "No survey row was both linked to a watch target and available before its 20:00 decision; "
                "no fatigue/caffeine/schedule correction or benefit estimate was fit."
                if no_eligible
                else "This v1 protocol did not prespecify a survey model once the measured zero-row assumption changed."
            ),
            "benefit_claimed": False,
        },
        "retrospective_associations": {
            "status": "association_only_not_predictive_or_causal",
            "fatigue_vs_device_interval": _pearson(fatigue_pairs),
            "caffeine_mg_vs_device_interval": _pearson(caffeine_pairs),
            "earliest_alarm_or_commitment_offset_vs_device_wake_offset": _pearson(schedule_pairs),
            "warning": (
                "These rows may have been backfilled after the night. Correlation is descriptive and is not evidence "
                "that adding the survey improves forecasts or changes sleep."
            ),
        },
        "production_engine_eligibility": {
            "eligible": False,
            "primary_source_required": "diary",
            "wearable_role": "auxiliary_only",
            "rested_field_present": any(audit.get("schema_has_rested") for audit in survey_file_audits),
            "good_day_rule": "rested>=4 and fatigue<=3",
            "reason": "The survey schema lacks rested; no value was fabricated and watch records were not relabeled as diary.",
        },
    }


def build_data_audit(
    workspace: Path,
    file_audits: Sequence[dict[str, Any]],
    dedup_audits: dict[str, dict[str, int]],
    episode_audits: dict[str, dict[str, int]],
    targets: Sequence[TargetNight],
    p01_audit: dict[str, Any],
    survey_summary: dict[str, Any],
) -> dict[str, Any]:
    participants: dict[str, Any] = {}
    for participant_id in ("P01", "P02", "P03", "P04"):
        rows = sorted(
            [target for target in targets if target.participant_id == participant_id],
            key=lambda row: row.cutoff,
        )
        participants[participant_id] = {
            "watch_target_count": len(rows),
            "split_counts": dict(sorted(Counter(row.split for row in rows).items())),
            "cutoff_min": rows[0].cutoff.isoformat() if rows else None,
            "cutoff_max": rows[-1].cutoff.isoformat() if rows else None,
            "low_total_n": len(rows) < 30,
            "deduplication": dedup_audits.get(participant_id),
            "episode_construction": episode_audits.get(participant_id),
        }
    return {
        "schema_version": 1,
        "workspace": relative_path(workspace, workspace),
        "raw_data_mode": "read_only",
        "raw_data_mutated": False,
        "identity": {
            "participant_ids": "stable Pxx IDs pinned to Samsung export IDs and survey filename locators",
            "folder_or_person_name_used": False,
            "unknown_export_policy": "fail_closed",
            "export_id_mapping": {
                participant_id: sorted(export_ids)
                for participant_id, export_ids in PARTICIPANT_EXPORT_IDS.items()
            },
            "known_misfiled_duplicate_handling": (
                "Export ID 20260919081592 always maps to P01 regardless of parent folder; identical rows are deduplicated."
            ),
        },
        "timezone": {
            "watch": (
                "Every Samsung row must carry time_offset. Supported naive sleep clocks are interpreted as UTC first, "
                "then converted to that explicit offset. No naive timestamp is assumed local."
            ),
            "model_contract": "+09:00 fixed; non-+09:00 episodes are audited and excluded rather than silently converted",
            "survey_date": "local study date at +09:00",
            "survey_saved_at": "explicit-offset instant (observed files use Z/UTC), compared as an instant",
        },
        "samsung_files": list(file_audits),
        "participants": participants,
        "p01_frozen_split": p01_audit,
        "survey": survey_summary,
        "target_qc": {
            "episode_merge_gap": "<=2 hours, matching existing preprocessing",
            "main_episode": "largest reported_sleep_min per local wake date",
            "common_window": "onset 0..960 and wake <=1440 minutes after local 20:00; interval 30..960",
            "target_definition": "consumer-device interval boundary, not PSG TST or optimal sleep need",
        },
    }


def source_record(path: Path, workspace: Path, role: str, **extra: Any) -> dict[str, Any]:
    record = {
        "path": relative_path(path, workspace),
        "role": role,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    record.update(extra)
    return record


def build_provenance(
    workspace: Path,
    sleep_files: Sequence[tuple[Path, str, str]],
    survey_paths: Sequence[Path],
    robust_paths: dict[str, Path],
    frozen_csv: Path,
    output_dir: Path,
) -> dict[str, Any]:
    source_files = [
        source_record(path, workspace, "raw_samsung_sleep", participant_id=pid, export_id=export_id)
        for path, pid, export_id in sleep_files
    ]
    source_files.extend(source_record(path, workspace, "raw_survey_checkin") for path in survey_paths)
    source_files.append(source_record(frozen_csv, workspace, "frozen_p01_196_night_split"))
    source_files.extend(
        source_record(path, workspace, "frozen_portable_robust_model", cohort=cohort)
        for cohort, path in sorted(robust_paths.items())
    )
    code_paths = {
        "evaluate": Path(__file__).resolve(),
        "night_core": workspace / "기타" / "SleepWell-integrated" / "web-app" / "night_core.py",
        "robust_features": workspace / "기타" / "SleepWell-integrated" / "web-app" / "robust_features.py",
        "portable_export_reference": workspace
        / "기타"
        / "SleepWell-integrated"
        / "tools"
        / "model"
        / "export_robust_portable.py",
    }
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "output_directory": relative_path(output_dir, workspace),
        "source_files": sorted(source_files, key=lambda row: (row["role"], row["path"])),
        "code": {
            name: {
                "path": relative_path(path, workspace),
                "sha256": sha256_file(path),
            }
            for name, path in sorted(code_paths.items())
        },
        "software": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "third_party_runtime_dependencies": [],
        },
        "portable_inference": {
            "implementation": "scalar exact counterpart of export_robust_portable.portable_predict",
            "artifacts_are_actual_exports": True,
            "fabricated_model_substitute": False,
            "personal_samsung_joblib": (
                "not loaded: no reviewed portable personal artifact; CMI/MESA exported artifacts are reported explicitly"
            ),
        },
        "write_policy": "all registered result paths checked absent first; each result then opened in exclusive create mode",
        "uploads_or_distribution": False,
        "product_or_model_promotion": False,
    }


def _csv_text(rows: Sequence[dict[str, Any]], fieldnames: Sequence[str]) -> str:
    import io

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fieldnames})
    return buffer.getvalue()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def ensure_outputs_absent(output_dir: Path) -> None:
    conflicts = [output_dir / name for name in RESULT_FILENAMES if (output_dir / name).exists()]
    if conflicts:
        joined = ", ".join(path.name for path in conflicts)
        raise FileExistsError(f"Refusing to overwrite existing result output(s): {joined}")


def write_outputs_exclusive(output_dir: Path, payloads: dict[str, str]) -> None:
    if set(payloads) != set(RESULT_FILENAMES):
        raise ValueError("Output payload set does not match registered result files")
    ensure_outputs_absent(output_dir)
    for filename in RESULT_FILENAMES:
        path = output_dir / filename
        with path.open("x", encoding="utf-8", newline="") as handle:
            handle.write(payloads[filename])


def run(workspace: Path, output_dir: Path) -> dict[str, Any]:
    personal_root = workspace / "보조데이터셋" / "개인데이터"
    frozen_csv = (
        workspace
        / "데이터모델"
        / "연구용데이터"
        / "next_night_run_20261020_v1"
        / "dataset"
        / "episodes.csv"
    )
    if not personal_root.is_dir():
        raise FileNotFoundError(personal_root)
    if not frozen_csv.is_file():
        raise FileNotFoundError(frozen_csv)

    sleep_files = discover_sleep_files(personal_root)
    sessions_by_participant: dict[str, list[SamsungSession]] = defaultdict(list)
    file_audits: list[dict[str, Any]] = []
    for path, participant_id, export_id in sleep_files:
        sessions, audit = read_samsung_sleep_csv(path, participant_id, export_id, workspace)
        sessions_by_participant[participant_id].extend(sessions)
        file_audits.append(audit)

    deduplicated: dict[str, list[SamsungSession]] = {}
    dedup_audits: dict[str, dict[str, int]] = {}
    episode_sets: dict[str, list[Episode]] = {}
    episode_audits: dict[str, dict[str, int]] = {}
    for participant_id in sorted(PARTICIPANT_EXPORT_IDS):
        rows, dedup_audit = deduplicate_sessions(sessions_by_participant.get(participant_id, []))
        deduplicated[participant_id] = rows
        dedup_audits[participant_id] = dedup_audit
        episodes, episode_audit = build_eligible_episodes(rows, participant_id)
        episode_sets[participant_id] = episodes
        episode_audits[participant_id] = episode_audit

    p01_targets, p01_audit = load_frozen_p01_targets(
        frozen_csv, episode_sets.get("P01", []), workspace
    )
    all_targets = p01_targets
    all_targets.extend(targets_from_episodes(episode_sets.get("P02", [])))
    all_targets.extend(targets_from_episodes(episode_sets.get("P03", [])))
    all_targets.sort(key=lambda row: (row.participant_id, row.cutoff))

    surveys, survey_file_audits, survey_paths = discover_and_read_surveys(personal_root, workspace)
    survey_summary = build_survey_summary(all_targets, surveys, survey_file_audits)

    night_core, robust_features = load_core_modules(workspace)
    robust_models, robust_paths = load_robust_models(workspace)
    predictions, constants, priors = evaluate_predictions(
        all_targets, night_core, robust_features, robust_models
    )
    metrics = summarize_metrics(all_targets, predictions, survey_summary)
    data_audit = build_data_audit(
        workspace,
        file_audits,
        dedup_audits,
        episode_audits,
        all_targets,
        p01_audit,
        survey_summary,
    )
    data_audit["train_only_priors"] = priors
    provenance = build_provenance(
        workspace, sleep_files, survey_paths, robust_paths, frozen_csv, output_dir
    )

    prediction_fields = (
        "participant_id",
        "target_id",
        "split",
        "cutoff",
        "sleep_start",
        "sleep_end",
        "observed_at",
        "arm",
        "predicted_onset_offset_min",
        "predicted_wake_offset_min",
        "predicted_interval_min",
        "actual_onset_offset_min",
        "actual_wake_offset_min",
        "actual_interval_min",
        "onset_abs_error_min",
        "wake_abs_error_min",
        "interval_abs_error_min",
        "joint_abs_error_min",
        "history_count",
        "max_history_observed_at",
        "prior_bed_offset_min",
        "prior_wake_offset_min",
        "model_status",
        "source_file_count",
        "source_record_count",
        "provenance_status",
    )
    constants_fields = (
        "participant_id",
        "target_id",
        "split",
        "cutoff",
        "history_count",
        "fixed_habit_bed_offset_min",
        "fixed_habit_wake_offset_min",
        "personal_mode",
        "personal_weight",
        "personal_habit_bed_offset_min",
        "personal_habit_wake_offset_min",
        "personal_phase_min",
        "personal_sleep_threshold",
        "personal_wake_threshold",
        "delta_personal_habit_bed_min",
        "delta_personal_habit_wake_min",
        "tau_wake_h",
        "tau_sleep_h",
        "physiological_tau_estimated",
        "interpretation",
    )
    payloads = {
        "predictions.csv": _csv_text(predictions, prediction_fields),
        "constants_history.csv": _csv_text(constants, constants_fields),
        "metrics.json": _json_text(metrics),
        "data_audit.json": _json_text(data_audit),
        "provenance.json": _json_text(provenance),
    }
    write_outputs_exclusive(output_dir, payloads)
    return {
        "participants_with_watch_targets": len({row.participant_id for row in all_targets}),
        "target_nights": len(all_targets),
        "prediction_rows": len(predictions),
        "strict_survey_watch_joins": survey_summary["strict_watch_join_rows"],
        "outputs": [str(output_dir / name) for name in RESULT_FILENAMES],
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    default_workspace = Path(__file__).resolve().parents[3]
    parser.add_argument("--workspace", type=Path, default=default_workspace)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    workspace = args.workspace.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Perform the overwrite gate before reading raw data or running inference.
    ensure_outputs_absent(output_dir)
    summary = run(workspace, output_dir)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
