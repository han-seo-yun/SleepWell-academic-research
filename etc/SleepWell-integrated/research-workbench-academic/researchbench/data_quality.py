"""Read-only data quality summaries for imported SleepWell research data."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


_FIELD_ALIASES = {
    "participant_id": ("participant_id", "subject_id"),
    "cutoff": ("cutoff",),
    "sleep_start": ("sleep_start", "bed"),
    "sleep_end": ("sleep_end", "wake"),
    "observed_at": ("observed_at",),
}
_TIMESTAMP_FIELDS = ("cutoff", "sleep_start", "sleep_end", "observed_at")
_RECENT_HISTORY_NIGHTS = 7
_PARTICIPANT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_KST = ZoneInfo("Asia/Seoul")


def _missing_mask(series: pd.Series) -> pd.Series:
    return series.isna() | series.map(lambda value: isinstance(value, str) and not value.strip())


def _timestamp(value: Any) -> pd.Timestamp:
    """Parse the offset-aware timestamp required by the executable model input."""
    if pd.isna(value) or not isinstance(value, str) or not value.strip():
        return pd.NaT
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        return pd.NaT
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return pd.NaT
    return pd.Timestamp(parsed)


def _field_mapping(frame: pd.DataFrame) -> dict[str, str | None]:
    return {
        role: next((column for column in aliases if column in frame.columns), None)
        for role, aliases in _FIELD_ALIASES.items()
    }


def summarize_quality(frame: pd.DataFrame) -> dict[str, Any]:
    """Describe quality without cleaning, dropping, or rewriting any values.

    The seven-night history count is informational. It describes coverage of the
    model's recent-7 feature window and is not treated as a minimum eligibility
    rule for every installed model.
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")

    mapping = _field_mapping(frame)
    missing_required = [role for role, column in mapping.items() if column is None]
    missing_required_values = {
        role: int(_missing_mask(frame[column]).sum())
        for role, column in mapping.items() if column is not None
    }
    missing_by_column = {
        str(column): int(_missing_mask(frame[column]).sum()) for column in frame.columns
    }
    total_cells = int(frame.shape[0] * frame.shape[1])
    missing_cells = sum(missing_by_column.values())
    columns = [
        {
            "name": str(column),
            "dtype": str(frame[column].dtype),
            "missing": missing_by_column[str(column)],
            "missing_fraction": (missing_by_column[str(column)] / len(frame)) if len(frame) else 0.0,
            "unique": int(frame[column].nunique(dropna=True)),
        }
        for column in frame.columns
    ]

    participant_column = mapping["participant_id"]
    participant_keys: pd.Series | None = None
    participants: int | None = None
    invalid_participant_ids = 0
    if participant_column:
        participant_keys = frame[participant_column].map(
            lambda value: None if pd.isna(value) or not str(value).strip() else str(value).strip()
        )
        if "cohort" in frame.columns:
            cohorts = frame["cohort"].map(
                lambda value: "" if pd.isna(value) else str(value).strip().upper()
            )
            participant_keys = pd.Series(
                [(cohort, participant) if participant is not None else None
                 for cohort, participant in zip(cohorts, participant_keys)],
                index=frame.index,
                dtype="object",
            )
        participants = int(participant_keys.dropna().nunique())
        invalid_participant_ids = sum(
            not (1 <= len(value) <= 64) or any(character not in _PARTICIPANT_CHARS for character in value)
            for value in frame[participant_column].dropna().map(str)
            if value.strip()
        )

    parsed: dict[str, pd.Series] = {}
    invalid_timestamps: dict[str, int] = {}
    for role in _TIMESTAMP_FIELDS:
        column = mapping[role]
        if column is None:
            continue
        parsed[role] = frame[column].map(_timestamp)
        invalid_timestamps[role] = int(parsed[role].isna().sum())

    nonpositive_intervals = 0
    duration_outside = 0
    if "sleep_start" in parsed and "sleep_end" in parsed:
        valid_pair = parsed["sleep_start"].notna() & parsed["sleep_end"].notna()
        nonpositive_intervals = int(
            (valid_pair & (parsed["sleep_end"] <= parsed["sleep_start"])).sum()
        )
        duration_outside = sum(
            not 30.0 <= (end - start).total_seconds() / 60.0 <= 960.0
            for start, end, valid in zip(parsed["sleep_start"], parsed["sleep_end"], valid_pair)
            if valid
        )

    cutoff_not_2000 = 0
    if "cutoff" in parsed:
        cutoff_not_2000 = sum(
            (local.hour, local.minute, local.second, local.microsecond) != (20, 0, 0, 0)
            for value in parsed["cutoff"] if not pd.isna(value)
            for local in [value.tz_convert(_KST)]
        )

    onset_outside = 0
    if "cutoff" in parsed and "sleep_start" in parsed:
        onset_outside = sum(
            not 0.0 <= (start - cutoff).total_seconds() / 60.0 <= 960.0
            for cutoff, start in zip(parsed["cutoff"], parsed["sleep_start"])
            if not pd.isna(cutoff) and not pd.isna(start)
        )

    wake_after_cutoff = 0
    if "cutoff" in parsed and "sleep_end" in parsed:
        wake_after_cutoff = sum(
            (end - cutoff).total_seconds() / 60.0 > 1440.0
            for cutoff, end in zip(parsed["cutoff"], parsed["sleep_end"])
            if not pd.isna(cutoff) and not pd.isna(end)
        )

    observed_before_wake = 0
    if "sleep_end" in parsed and "observed_at" in parsed:
        valid_pair = parsed["sleep_end"].notna() & parsed["observed_at"].notna()
        observed_before_wake = int(
            (valid_pair & (parsed["observed_at"] < parsed["sleep_end"])).sum()
        )

    duplicate_keys = 0
    participants_below_history: int | None = None
    if participant_keys is not None and mapping["cutoff"]:
        key_frame = pd.DataFrame({"participant": participant_keys, "cutoff": frame[mapping["cutoff"]]})
        usable_keys = key_frame[~key_frame["participant"].isna() & ~_missing_mask(key_frame["cutoff"])]
        duplicate_keys = int(usable_keys.duplicated(["participant", "cutoff"]).sum())
        nights = usable_keys.groupby("participant", dropna=True)["cutoff"].nunique()
        participants_below_history = int((nights < _RECENT_HISTORY_NIGHTS).sum())

    exact_duplicates = int(frame.duplicated().sum())
    findings: list[dict[str, Any]] = []

    def finding(code: str, severity: str, count: int, detail: str) -> None:
        if count:
            findings.append({"code": code, "severity": severity, "count": int(count), "detail": detail})

    finding("empty_dataset", "error", int(frame.empty), "At least one row is required.")
    finding("missing_required_columns", "error", len(missing_required),
            ", ".join(missing_required))
    finding("missing_required_values", "error", sum(missing_required_values.values()),
            "Blank or null values in mapped SleepWell fields.")
    finding("missing_cells", "warning", missing_cells, "Blank and null cells are counted; no values were changed.")
    finding("exact_duplicate_rows", "warning", exact_duplicates, "Exact repeated rows.")
    finding("duplicate_participant_cutoff", "error", duplicate_keys,
            "The executable model requires unique participant and cutoff pairs.")
    finding("invalid_participant_id", "error", invalid_participant_ids,
            "Participant IDs must be 1-64 ASCII letters, digits, period, underscore, or hyphen.")
    finding("invalid_timestamps", "error", sum(invalid_timestamps.values()),
            "Blank, invalid, or timezone-naive timestamps across required time fields.")
    finding("cutoff_not_2000_kst", "error", cutoff_not_2000,
            "Cutoff must be exactly 20:00:00 Asia/Seoul.")
    finding("onset_outside_supported_horizon", "error", onset_outside,
            "Sleep start must be 0-960 minutes after cutoff.")
    finding("nonpositive_sleep_intervals", "error", nonpositive_intervals,
            "Sleep end is at or before sleep start.")
    finding("sleep_duration_outside_30_960_min", "error", duration_outside,
            "Sleep duration must be 30-960 minutes.")
    finding("wake_after_cutoff_1440_min", "error", wake_after_cutoff,
            "Sleep end must be no later than 1,440 minutes after cutoff.")
    finding("observed_before_sleep_end", "error", observed_before_wake,
            "Observation time precedes sleep end.")
    if participants_below_history is not None:
        finding("history_below_recent7_window", "info", participants_below_history,
                "Participants with fewer than seven unique cutoffs; this is coverage context, not universal exclusion.")

    return {
        "rows": int(len(frame)),
        "column_count": int(len(frame.columns)),
        "participants": participants,
        "missing_cells": int(missing_cells),
        "missing_cell_fraction": (missing_cells / total_cells) if total_cells else 0.0,
        "exact_duplicate_rows": exact_duplicates,
        "duplicate_participant_cutoffs": duplicate_keys,
        "field_mapping": mapping,
        "missing_required_fields": missing_required,
        "missing_required_values": missing_required_values,
        "invalid_timestamps": invalid_timestamps,
        "invalid_participant_ids": invalid_participant_ids,
        "cutoff_not_2000_kst": cutoff_not_2000,
        "onset_outside_supported_horizon": onset_outside,
        "nonpositive_sleep_intervals": nonpositive_intervals,
        "sleep_duration_outside_30_960_min": duration_outside,
        "wake_after_cutoff_1440_min": wake_after_cutoff,
        "observed_before_sleep_end": observed_before_wake,
        "recent_history_window_nights": _RECENT_HISTORY_NIGHTS,
        "participants_below_7_nights": participants_below_history,
        "columns": columns,
        "findings": findings,
        "sleepwell_ready": not any(item["severity"] == "error" for item in findings),
    }


def load_project_quality(project: Any) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load and verify the active immutable project snapshot, then summarize it."""
    item = project.state.get("input")
    if not item:
        raise ValueError("Project has no imported input snapshot")
    path = project.snapshot_path(item["snapshot"])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != item.get("sha256"):
        raise ValueError("Input snapshot SHA256 mismatch")
    frame = pd.read_csv(path)
    summary = summarize_quality(frame)
    summary["source"] = {
        "name": item.get("source_name"),
        "sha256": digest,
        "synthetic": bool(item.get("synthetic")),
    }
    return frame, summary


__all__ = ["load_project_quality", "summarize_quality"]
