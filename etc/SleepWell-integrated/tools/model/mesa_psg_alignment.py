#!/usr/bin/env python3
"""Align MESA actigraphy epochs to NSRR polysomnography stage annotations."""

from __future__ import annotations

import bisect
import csv
import hashlib
import io
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np


FEATURE_NAMES = ("activity", "whitelight", "redlight", "greenlight", "bluelight")
COMMON_BLOCK_ROWS = 20
SECONDS_PER_DAY = 24 * 60 * 60
UNKNOWN_ACTIGRAPHY_LABEL = 255
MAX_SENSOR_VALUE = float(np.finfo(np.float32).max)
_REQUIRED_OVERLAP = {"mesaid", "line", "linetime", "starttime_psg"}
_REQUIRED_ACT = {"mesaid", "line", "linetime", "offwrist", *FEATURE_NAMES}


def _normalize_participant(value: object) -> str:
    text = str(value).strip()
    if not text.isdigit():
        raise ValueError("participant ID is not numeric")
    return str(int(text))


def _clock_seconds(value: object, field: str) -> int:
    text = str(value).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", text)
    if not match:
        raise ValueError(f"invalid {field}: expected HH:MM:SS")
    hour, minute, second = map(int, match.groups())
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError(f"invalid {field}: clock component out of range")
    return hour * 3600 + minute * 60 + second


def _format_clock(seconds: int) -> str:
    hour, remainder = divmod(seconds, 3600)
    minute, second = divmod(remainder, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def _circular_seconds(left: int, right: int) -> int:
    """Return the nearest signed clock difference, left minus right."""
    difference = (left - right) % SECONDS_PER_DAY
    if difference > SECONDS_PER_DAY // 2:
        difference -= SECONDS_PER_DAY
    return difference


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _read_stable_bytes(path: Path) -> tuple[bytes, str]:
    before = _signature(path)
    content = path.read_bytes()
    after = _signature(path)
    if before != after or len(content) != before[0]:
        raise RuntimeError(f"source changed while being read: {path}")
    return content, hashlib.sha256(content).hexdigest()


def read_overlap(path: str | Path) -> dict[str, dict]:
    """Read the official ACT/PSG anchor table, rejecting malformed duplicates."""
    source = Path(path)
    content, _ = _read_stable_bytes(source)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("overlap table is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not _REQUIRED_OVERLAP.issubset(set(reader.fieldnames or ())):
        raise ValueError("required overlap columns are missing")
    result: dict[str, dict] = {}
    for row_number, row in enumerate(reader, start=2):
        try:
            participant = _normalize_participant(row.get("mesaid", ""))
            line = int(str(row.get("line", "")).strip())
            linetime_seconds = _clock_seconds(row.get("linetime", ""), "linetime")
            starttime_seconds = _clock_seconds(row.get("starttime_psg", ""), "starttime_psg")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"malformed overlap row {row_number}: {exc}") from exc
        if line < 1 or line > np.iinfo(np.int32).max:
            raise ValueError(f"malformed overlap row {row_number}: line out of range")
        if participant in result:
            raise ValueError(f"duplicate overlap subject: {participant}")
        result[participant] = {
            "mesaid": participant,
            "line": line,
            "linetime": _format_clock(linetime_seconds),
            "starttime_psg": _format_clock(starttime_seconds),
        }
    return result


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _stage_label(concept: str) -> int | None:
    suffix = concept.rsplit("|", 1)[-1].strip()
    name = concept.split("|", 1)[0].strip().lower()
    if suffix == "0" and name in {"wake", "stage wake", "sleep stage w"}:
        return 0
    if suffix in {"1", "2", "3", "4", "5"}:
        return 1
    return None


def _recording_clock_seconds(value: str) -> int:
    clock = value.strip().split()[-1] if value.strip() else ""
    return _clock_seconds(clock.replace(".", ":"), "Recording Start Time ClockTime")


def parse_stages(xml_path: str | Path) -> tuple[list[tuple[float, float, int | None]], dict]:
    """Parse NSRR stage intervals and provenance metadata.

    Unknown stage concepts are retained with a ``None`` label so that alignment
    cannot silently treat them as wake or sleep.
    """
    source = Path(xml_path)
    content, source_hash = _read_stable_bytes(source)
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("invalid NSRR XML") from exc

    epoch_nodes = [node for node in root.iter() if _local_name(node.tag) == "EpochLength"]
    if len(epoch_nodes) != 1:
        raise ValueError("NSRR XML must contain exactly one EpochLength")
    try:
        epoch_length = float((epoch_nodes[0].text or "").strip())
    except ValueError as exc:
        raise ValueError("invalid NSRR EpochLength") from exc
    if not math.isfinite(epoch_length) or epoch_length != 30.0:
        raise ValueError("NSRR EpochLength must be 30 seconds")

    intervals: list[tuple[float, float, int | None]] = []
    recording_events: list[ET.Element] = []
    for event in root.iter():
        if _local_name(event.tag) != "ScoredEvent":
            continue
        event_type = _child_text(event, "EventType")
        concept = _child_text(event, "EventConcept")
        if concept.strip().lower() == "recording start time":
            recording_events.append(event)
        if event_type.split("|", 1)[0].strip().lower() != "stages":
            continue
        try:
            start = float(_child_text(event, "Start"))
            duration = float(_child_text(event, "Duration"))
        except ValueError as exc:
            raise ValueError("stage event has invalid Start or Duration") from exc
        if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration <= 0:
            raise ValueError("stage event has invalid bounds")
        if abs(start / 30.0 - round(start / 30.0)) > 1e-9 or abs(duration / 30.0 - round(duration / 30.0)) > 1e-9:
            raise ValueError("stage event bounds must align to 30-second epochs")
        intervals.append((start, start + duration, _stage_label(concept)))

    if not intervals:
        raise ValueError("NSRR XML contains no stage events")
    if len(recording_events) != 1:
        raise ValueError("NSRR XML must contain exactly one Recording Start Time event")
    recording_event = recording_events[0]
    try:
        recording_start = float(_child_text(recording_event, "Start"))
        recording_duration = float(_child_text(recording_event, "Duration"))
        recording_clock = _recording_clock_seconds(_child_text(recording_event, "ClockTime"))
    except ValueError as exc:
        raise ValueError("invalid Recording Start Time event") from exc
    if (
        not math.isfinite(recording_start)
        or recording_start != 0.0
        or not math.isfinite(recording_duration)
        or recording_duration <= 0
    ):
        raise ValueError("Recording Start Time must have Start=0 and positive finite Duration")
    intervals.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1] - 1e-9:
            raise ValueError("overlapping NSRR stage events")
    known_intervals = [item for item in intervals if item[2] is not None]
    annotated_start = min(item[0] for item in known_intervals) if known_intervals else 0.0
    annotated_end = max(item[1] for item in known_intervals) if known_intervals else 0.0
    recording_epoch_limit = math.ceil(recording_duration / 30.0) * 30.0
    if annotated_end > recording_epoch_limit + 1e-9:
        raise ValueError("stage annotations extend beyond recording duration")
    metadata = {
        "epoch_length_seconds": 30,
        "stage_intervals": len(intervals),
        "unknown_stage_intervals": sum(label is None for _, _, label in intervals),
        "recording_start_clock_seconds": recording_clock,
        "recording_duration_seconds": recording_duration,
        "recording_epoch_limit_seconds": recording_epoch_limit,
        "annotated_start_seconds": annotated_start,
        "annotated_end_seconds": annotated_end,
        "annotated_span_seconds": annotated_end - annotated_start,
        "source_sha256": source_hash,
    }
    return intervals, metadata


def _finite_sensor(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= MAX_SENSOR_VALUE else None


def _label_interval(
    start: float,
    intervals: list[tuple[float, float, int | None]],
    starts: list[float],
) -> tuple[int | None, str | None]:
    end = start + 30.0
    index = max(0, bisect.bisect_right(starts, start) - 1)
    covered = 0.0
    durations = [0.0, 0.0]
    cursor = start
    while index < len(intervals) and intervals[index][0] < end - 1e-9:
        interval_start, interval_end, label = intervals[index]
        overlap_start = max(start, interval_start)
        overlap_end = min(end, interval_end)
        if overlap_end > overlap_start + 1e-9:
            if overlap_start > cursor + 1e-9:
                return None, "incomplete_psg_coverage"
            if label is None:
                return None, "unknown_psg_stage"
            duration = overlap_end - overlap_start
            durations[label] += duration
            covered += duration
            cursor = max(cursor, overlap_end)
        index += 1
    if covered < 30.0 - 1e-9 or cursor < end - 1e-9:
        return None, "incomplete_psg_coverage"
    if abs(durations[0] - durations[1]) <= 1e-9:
        return None, "psg_duration_tie"
    return int(durations[1] > durations[0]), None


def _participant_from_filename(path: Path) -> str | None:
    match = re.search(r"mesa-sleep-(\d+)(?:-nsrr)?\.(?:csv|xml)$", path.name, re.IGNORECASE)
    return _normalize_participant(match.group(1)) if match else None


def align_subject(
    act_path: str | Path,
    xml_path: str | Path,
    overlap_row: dict,
    participant: str,
) -> tuple[dict[str, np.ndarray], dict, dict[str, str]]:
    """Align one participant and keep only complete valid line-aligned blocks."""
    expected_participant = _normalize_participant(participant)
    try:
        overlap_participant = _normalize_participant(overlap_row["mesaid"])
        anchor_line = int(overlap_row["line"])
        anchor_clock = _clock_seconds(overlap_row["linetime"], "linetime")
        psg_start_clock = _clock_seconds(overlap_row["starttime_psg"], "starttime_psg")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed overlap row: {exc}") from exc
    if overlap_participant != expected_participant:
        raise ValueError("overlap participant does not match requested participant")
    if anchor_line < 1:
        raise ValueError("overlap anchor line must be positive")
    psg_anchor_residual = _circular_seconds(anchor_clock, psg_start_clock)
    if abs(psg_anchor_residual) > 15:
        raise ValueError("overlap PSG start differs from anchor linetime by more than 15 seconds")

    act_source = Path(act_path)
    xml_source = Path(xml_path)
    for source in (act_source, xml_source):
        filename_participant = _participant_from_filename(source)
        if filename_participant is not None and filename_participant != expected_participant:
            raise ValueError("source filename participant does not match requested participant")

    intervals, stage_metadata = parse_stages(xml_source)
    if stage_metadata["recording_start_clock_seconds"] != psg_start_clock:
        raise ValueError("XML Recording Start Time does not match overlap starttime_psg")
    stage_starts = [item[0] for item in intervals]
    act_content, act_hash = _read_stable_bytes(act_source)
    try:
        act_text = act_content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("actigraphy CSV is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(act_text, newline=""))
    header = set(reader.fieldnames or ())
    if not _REQUIRED_ACT.issubset(header):
        raise ValueError("required MESA actigraphy columns are missing")

    records: list[dict] = []
    previous_line: int | None = None
    previous_clock: int | None = None
    anchor_actual_clock: int | None = None
    qc: Counter[str] = Counter()
    for row_number, row in enumerate(reader, start=2):
        qc["source_rows"] += 1
        try:
            line = int((row.get("line") or "").strip())
            clock = _clock_seconds(row.get("linetime", ""), "linetime")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid ACT row {row_number}: {exc}") from exc
        if line < 1 or line > np.iinfo(np.int32).max:
            raise ValueError(f"invalid ACT line at row {row_number}")
        if previous_line is not None:
            line_delta = line - previous_line
            if line_delta <= 0:
                raise ValueError("ACT line is not strictly increasing and unique")
            if (clock - previous_clock) % SECONDS_PER_DAY != (line_delta * 30) % SECONDS_PER_DAY:
                raise ValueError("ACT line/time cadence is not consistently 30 seconds")
        previous_line, previous_clock = line, clock
        if line == anchor_line:
            anchor_actual_clock = clock

        try:
            content_participant = _normalize_participant(row.get("mesaid", ""))
        except ValueError:
            content_participant = ""
        values = [_finite_sensor(row.get(name, "")) for name in FEATURE_NAMES]
        eligible = True
        if content_participant != expected_participant:
            qc["participant_mismatch_rows"] += 1
            eligible = False
        if (row.get("offwrist") or "").strip() != "0":
            qc["offwrist_rows"] += 1
            eligible = False
        if any(value is None for value in values):
            qc["invalid_sensor_rows"] += 1
            eligible = False

        actigraphy_label = UNKNOWN_ACTIGRAPHY_LABEL
        wake = (row.get("wake") or "").strip()
        if wake in {"0", "1"}:
            actigraphy_label = 1 - int(wake)

        # The official overlap row maps PSG epoch zero to anchor_line.  The
        # sub-epoch clock residual is validation evidence, not a time shift.
        t_psg = (line - anchor_line) * 30
        psg_label, psg_exclusion = _label_interval(float(t_psg), intervals, stage_starts)
        if psg_exclusion is not None:
            qc[psg_exclusion + "_rows"] += 1
            eligible = False
        else:
            qc["psg_scored_rows"] += 1
        records.append({
            "line": line,
            "features": [float(value) if value is not None else math.nan for value in values],
            "psg_label": psg_label,
            "actigraphy_label": actigraphy_label,
            "eligible": eligible,
        })

    if not records:
        raise ValueError("actigraphy CSV contains no rows")
    if anchor_actual_clock is None:
        raise ValueError("ACT data does not contain the overlap anchor line")
    anchor_error = _circular_seconds(anchor_actual_clock, anchor_clock)
    if anchor_error != 0:
        raise ValueError("ACT anchor linetime does not exactly match overlap linetime")

    blocks: dict[int, list[dict]] = defaultdict(list)
    for record in records:
        blocks[(record["line"] - 1) // COMMON_BLOCK_ROWS].append(record)
    retained: list[dict] = []
    for block_id in sorted(blocks):
        block = blocks[block_id]
        expected_lines = list(range(block_id * COMMON_BLOCK_ROWS + 1, (block_id + 1) * COMMON_BLOCK_ROWS + 1))
        if [record["line"] for record in block] == expected_lines and all(record["eligible"] for record in block):
            retained.extend(block)
            qc["eligible_ten_minute_blocks"] += 1
        else:
            qc["excluded_incomplete_or_invalid_ten_minute_blocks"] += 1

    first_line = records[0]["line"]
    arrays = {
        "line": np.asarray([row["line"] for row in retained], dtype=np.int32),
        "t_rel_sec": np.asarray([(row["line"] - first_line) * 30 for row in retained], dtype=np.int64),
        "x": np.asarray([row["features"] for row in retained], dtype=np.float32).reshape((-1, len(FEATURE_NAMES))),
        "y_sleep": np.asarray([row["psg_label"] for row in retained], dtype=np.uint8),
        "y_actigraphy": np.asarray([row["actigraphy_label"] for row in retained], dtype=np.uint8),
    }
    qc["rows_passing_row_filters"] = sum(record["eligible"] for record in records)
    qc["eligible_native_rows"] = len(retained)
    qc["excluded_by_block_requirement_rows"] = qc["rows_passing_row_filters"] - len(retained)
    qc["anchor_clock_error_seconds"] = anchor_error
    qc["psg_anchor_offset_seconds"] = psg_anchor_residual
    qc["stage_intervals"] = stage_metadata["stage_intervals"]
    qc["psg_unscored_rows"] = qc["source_rows"] - qc["psg_scored_rows"]
    qc["recording_duration_seconds"] = stage_metadata["recording_duration_seconds"]
    qc["annotated_span_seconds"] = stage_metadata["annotated_span_seconds"]

    overlap_canonical = (
        f"{expected_participant},{anchor_line},{_format_clock(anchor_clock)},"
        f"{_format_clock(psg_start_clock)}\n"
    ).encode("ascii")
    source_hashes = {
        "actigraphy_sha256": act_hash,
        "xml_sha256": stage_metadata["source_sha256"],
        "overlap_row_sha256": hashlib.sha256(overlap_canonical).hexdigest(),
    }
    return arrays, dict(sorted(qc.items())), source_hashes
