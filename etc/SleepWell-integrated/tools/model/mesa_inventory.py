#!/usr/bin/env python3
"""Create a privacy-preserving, bounded inventory of a local MESA export."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Iterable


PARTICIPANT_PATTERN = re.compile(r"mesa-sleep-(\d+)", re.IGNORECASE)
EXPECTED_BINARY = {"", "0", "1"}
NUMERIC_COLUMNS = {"activity", "whitelight", "redlight", "greenlight", "bluelight"}
NAMED_MODALITIES = ("actigraphy", "heart_rate", "steps", "psg_edf", "psg_annotation")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stat_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _modality(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    text = "/".join(part.lower() for part in relative.parts)
    suffix = path.suffix.lower()
    if suffix == ".edf":
        return "psg_edf"
    if suffix in {".xml", ".annot", ".annotation"} or "annotation" in text:
        return "psg_annotation"
    if "actigraphy" in text:
        return "actigraphy"
    if any(token in text for token in ("heart_rate", "heartrate", "heart-rate")):
        return "heart_rate"
    if "steps" in text:
        return "steps"
    return "other"


def _participant_id(path: Path) -> str | None:
    match = PARTICIPANT_PATTERN.search(path.name)
    return str(int(match.group(1))) if match else None


def _safe_regular_files(root: Path) -> tuple[list[Path], dict[str, int]]:
    files: list[Path] = []
    skipped = Counter()
    for path in root.rglob("*"):
        if path.is_symlink():
            skipped["symlinks"] += 1
            continue
        try:
            if not path.is_file():
                continue
            resolved = path.resolve(strict=True)
        except OSError:
            skipped["unreadable_or_disappeared"] += 1
            continue
        if not _is_within(resolved, root):
            skipped["resolved_outside_root"] += 1
            continue
        files.append(path)
    files.sort(key=lambda item: item.relative_to(root).as_posix())
    return files, dict(sorted(skipped.items()))


def _even_sample(items: list[Path], count: int) -> list[Path]:
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[0]]
    indices = [(index * (len(items) - 1)) // (count - 1) for index in range(count)]
    return [items[index] for index in indices]


def _safe_category(value: str) -> str:
    value = value.strip()
    if not value:
        return "<missing>"
    if len(value) <= 32 and re.fullmatch(r"[A-Za-z0-9_.+\-/]+", value):
        return value
    return "<unexpected>"


def _is_number(value: str) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _parse_time(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_header(path: Path) -> tuple[str, ...] | None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return tuple(next(csv.reader(handle)))
    except (OSError, StopIteration, UnicodeDecodeError, csv.Error):
        return None


def _inspect_csv(path: Path, expected_id: str | None, max_rows: int) -> dict:
    missing: Counter[str] = Counter()
    categories = {name: Counter() for name in ("wake", "interval", "offwrist")}
    invalid: Counter[str] = Counter()
    rows = 0
    previous_line: int | None = None
    previous_time: datetime | None = None
    cadence_seconds: Counter[int] = Counter()
    truncated = False

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = tuple(reader.fieldnames or ())
            for row in reader:
                if rows >= max_rows:
                    truncated = True
                    break
                rows += 1
                if None in row:
                    invalid["extra_fields"] += len(row[None])
                for column in columns:
                    value = (row.get(column) or "").strip()
                    if not value:
                        missing[column] += 1
                for column in categories:
                    categories[column][_safe_category(row.get(column) or "")] += 1
                for column in ("wake", "offwrist"):
                    if (row.get(column) or "").strip() not in EXPECTED_BINARY:
                        invalid[f"{column}_not_binary"] += 1
                for column in NUMERIC_COLUMNS:
                    value = (row.get(column) or "").strip()
                    if value and not _is_number(value):
                        invalid[f"{column}_not_numeric"] += 1
                if expected_id is not None:
                    row_id = (row.get("mesaid") or "").strip().lstrip("0") or "0"
                    normalized_expected = expected_id.lstrip("0") or "0"
                    if row_id != normalized_expected:
                        invalid["filename_content_id_mismatch"] += 1
                line_value = (row.get("line") or "").strip()
                try:
                    line = int(line_value)
                except ValueError:
                    invalid["line_not_integer"] += 1
                else:
                    if previous_line is not None and line != previous_line + 1:
                        invalid["line_not_sequential"] += 1
                    previous_line = line
                time_value = (row.get("linetime") or "").strip()
                date_value = (row.get("date_shifted") or "").strip()
                combined_time = f"{date_value}T{time_value}" if date_value and time_value else time_value
                parsed_time = _parse_time(combined_time)
                if parsed_time is None:
                    invalid["linetime_invalid"] += 1
                else:
                    if previous_time is not None:
                        seconds = int((parsed_time - previous_time).total_seconds())
                        cadence_seconds[seconds] += 1
                        if seconds <= 0:
                            invalid["linetime_not_increasing"] += 1
                    previous_time = parsed_time
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return {
            "read_error": type(exc).__name__,
            "rows": rows,
            "truncated_at_row_limit": truncated,
        }

    return {
        "rows": rows,
        "truncated_at_row_limit": truncated,
        "missing": dict(sorted(missing.items())),
        "invalid": dict(sorted(invalid.items())),
        "categories": {name: dict(sorted(counts.items())) for name, counts in categories.items()},
        "cadence_seconds": {str(key): value for key, value in sorted(cadence_seconds.items())},
    }


def _sum_nested_counter(target: Counter, values: dict[str, int]) -> None:
    for key, value in values.items():
        target[key] += value


def build_inventory(
    root: Path,
    *,
    sample_files: int = 12,
    max_rows_per_file: int = 100_000,
    max_header_files: int = 10_000,
) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("--root must be a directory")
    if min(sample_files, max_rows_per_file, max_header_files) < 1:
        raise ValueError("sampling limits must be positive integers")

    discovered_at = _utc_now()
    snapshot, skipped_at_discovery = _safe_regular_files(root)
    before = {path: _stat_signature(path) for path in snapshot}
    extension_counts = Counter((path.suffix.lower() or "<none>") for path in snapshot)
    modality_files: dict[str, list[Path]] = {name: [] for name in (*NAMED_MODALITIES, "other")}
    participant_sets: dict[str, set[str]] = {name: set() for name in (*NAMED_MODALITIES, "other")}
    unmatched_ids = Counter()
    for path in snapshot:
        modality = _modality(path, root)
        modality_files[modality].append(path)
        participant = _participant_id(path)
        if participant is None:
            unmatched_ids[modality] += 1
        else:
            participant_sets[modality].add(participant)

    header_candidates = [
        path for path in snapshot if path.suffix.lower() == ".csv"
    ][:max_header_files]
    header_counts: Counter[tuple[str, ...] | None] = Counter(_read_header(path) for path in header_candidates)
    header_variants = []
    for header, count in sorted(header_counts.items(), key=lambda item: (-item[1], str(item[0]))):
        header_variants.append({"columns": list(header) if header is not None else None, "files": count})

    eligible = [
        path
        for path in modality_files["actigraphy"]
        if path.suffix.lower() == ".csv" and before[path] is not None
    ]
    sampled = _even_sample(eligible, sample_files)
    aggregate_rows = 0
    complete_files = 0
    truncated_files = 0
    read_error_files = 0
    unstable_sample_files = 0
    aggregate_missing: Counter[str] = Counter()
    aggregate_invalid: Counter[str] = Counter()
    aggregate_categories = {name: Counter() for name in ("wake", "interval", "offwrist")}
    aggregate_cadence: Counter[str] = Counter()

    for path in sampled:
        result = _inspect_csv(path, _participant_id(path), max_rows_per_file)
        after = _stat_signature(path)
        if after != before[path]:
            unstable_sample_files += 1
            continue
        if "read_error" in result:
            read_error_files += 1
            continue
        if result["truncated_at_row_limit"]:
            truncated_files += 1
            continue
        complete_files += 1
        aggregate_rows += result["rows"]
        _sum_nested_counter(aggregate_missing, result["missing"])
        _sum_nested_counter(aggregate_invalid, result["invalid"])
        for name in aggregate_categories:
            _sum_nested_counter(aggregate_categories[name], result["categories"][name])
        _sum_nested_counter(aggregate_cadence, result["cadence_seconds"])

    ending_files, skipped_at_completion = _safe_regular_files(root)
    ending_set = set(ending_files)
    snapshot_set = set(snapshot)
    changed_snapshot_files = sum(
        1 for path in snapshot if path in ending_set and _stat_signature(path) != before[path]
    )
    disappeared_files = len(snapshot_set - ending_set)
    added_files = len(ending_set - snapshot_set)

    modality_summary = {
        name: {
            "files": len(modality_files[name]),
            "unique_participant_ids": len(participant_sets[name]),
            "files_without_detectable_participant_id": unmatched_ids[name],
        }
        for name in (*NAMED_MODALITIES, "other")
    }
    intersections = {
        f"{left}&{right}": len(participant_sets[left] & participant_sets[right])
        for left, right in combinations(NAMED_MODALITIES, 2)
    }
    named_sets = [participant_sets[name] for name in NAMED_MODALITIES]
    intersections["all_named_modalities"] = len(set.intersection(*named_sets)) if named_sets else 0

    cadence_total = sum(aggregate_cadence.values())
    cadence_30 = aggregate_cadence.get("30", 0)
    offwrist_total = sum(aggregate_categories["offwrist"].values())
    offwrist_flagged = aggregate_categories["offwrist"].get("1", 0)

    return {
        "schema_version": 1,
        "scope": {
            "discovered_at_utc": discovered_at,
            "completed_at_utc": _utc_now(),
            "snapshot_file_count": len(snapshot),
            "ending_file_count": len(ending_files),
            "files_added_during_scan": added_files,
            "files_disappeared_during_scan": disappeared_files,
            "snapshot_files_changed_during_scan": changed_snapshot_files,
            "skipped_at_discovery": skipped_at_discovery,
            "skipped_at_completion": skipped_at_completion,
            "active_ingestion_detected": bool(added_files or disappeared_files or changed_snapshot_files),
            "integrity_claim": "bounded_inventory_not_exhaustive_file_integrity_validation",
        },
        "inventory": {
            "total_bytes_at_discovery": sum(signature[0] for signature in before.values() if signature),
            "extension_counts": dict(sorted(extension_counts.items())),
            "modalities": modality_summary,
            "participant_intersections": intersections,
        },
        "headers": {
            "eligible_csv_files": sum(1 for path in snapshot if path.suffix.lower() == ".csv"),
            "scanned_files": len(header_candidates),
            "scan_limit": max_header_files,
            "truncated": len(header_candidates) < sum(1 for path in snapshot if path.suffix.lower() == ".csv"),
            "variants": header_variants,
        },
        "semantic_qc": {
            "sampling": {
                "strategy": "deterministic_even_spacing_over_sorted_actigraphy_csv_snapshot",
                "requested_files": sample_files,
                "selected_files": len(sampled),
                "stable_complete_files_in_aggregate": complete_files,
                "unstable_files_excluded": unstable_sample_files,
                "row_limit_exceeded_files_excluded": truncated_files,
                "read_error_files_excluded": read_error_files,
                "max_rows_per_file": max_rows_per_file,
            },
            "rows": aggregate_rows,
            "missing_by_column": dict(sorted(aggregate_missing.items())),
            "invalid_fields": dict(sorted(aggregate_invalid.items())),
            "categories": {
                name: dict(sorted(counter.items())) for name, counter in aggregate_categories.items()
            },
            "cadence_seconds": dict(sorted(aggregate_cadence.items(), key=lambda item: int(item[0]))),
            "thirty_second_cadence_fraction": cadence_30 / cadence_total if cadence_total else None,
            "offwrist_flagged_fraction": offwrist_flagged / offwrist_total if offwrist_total else None,
        },
        "interpretation": {
            "wake_and_interval_are_derived_actigraphy_labels_not_psg_sleep_stages": True,
            "psg_matched_file_candidates": len(
                participant_sets["psg_edf"] & participant_sets["psg_annotation"]
            ),
            "psg_matched_file_candidates_present": bool(
                participant_sets["psg_edf"] & participant_sets["psg_annotation"]
            ),
            "psg_supervised_stage_training_ready": False,
            "psg_readiness_requires": [
                "EDF and annotation content validation",
                "recording and epoch alignment validation",
                "sleep-stage label mapping validation",
                "signal and annotation quality validation",
            ],
            "source_epoch_seconds_expected": 30,
            "sixty_second_features_require_separate_causal_aggregation": True,
        },
    }


def write_inventory(payload: dict, output: Path, root: Path) -> None:
    root = root.resolve(strict=True)
    output = output.resolve()
    if _is_within(output, root):
        raise ValueError("--output must be outside --root")
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary_name, output)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Local MESA dataset root")
    parser.add_argument("--output", type=Path, required=True, help="JSON summary path outside the dataset root")
    parser.add_argument("--sample-files", type=int, default=12)
    parser.add_argument("--max-rows-per-file", type=int, default=100_000)
    parser.add_argument("--max-header-files", type=int, default=10_000)
    args = parser.parse_args()
    payload = build_inventory(
        args.root,
        sample_files=args.sample_files,
        max_rows_per_file=args.max_rows_per_file,
        max_header_files=args.max_header_files,
    )
    write_inventory(payload, args.output, args.root)
    print(json.dumps({"output_written": True, "snapshot_file_count": payload["scope"]["snapshot_file_count"]}))


if __name__ == "__main__":
    main()
