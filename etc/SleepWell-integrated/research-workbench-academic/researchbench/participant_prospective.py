"""Participant-isolated wrapper over the original single-participant adapter.

The original journal and forecasting policy remain unchanged.  This module
adds a higher-level boundary: each participant receives a separate journal
whose filename contains only a deterministic SHA-256 identifier.  Synthetic
multi-participant files are split and replayed through the original adapter one
participant at a time, then summarized with equal participant weighting.
"""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from .academic_prospective import list_forecasts, run_prospective


def participant_key(participant_id: Any) -> str:
    text = str(participant_id).strip()
    if not text:
        raise ValueError("participant_id is required")
    return "participant_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("synthetic CSV has no header")
        return list(reader.fieldnames), [dict(row) for row in reader]


def _filtered_records(config: Mapping[str, Any], raw_id: str, key: str) -> tuple[dict[str, Any], int]:
    copied = deepcopy(dict(config))
    records: list[dict[str, Any]] | None = None
    if "records" in copied:
        if not isinstance(copied["records"], list):
            raise ValueError("records must be a list")
        records = [dict(row) for row in copied.pop("records")]
    elif copied.get("csv_file"):
        _, records = _read_csv(Path(copied.pop("csv_file")).expanduser().resolve())
    excluded = 0
    if records is not None:
        selected = []
        for row in records:
            supplied = row.get("participant_id") or row.get("enrollment_id")
            if supplied not in (None, "") and str(supplied) != raw_id:
                excluded += 1
                continue
            item = dict(row)
            item["participant_id"] = key
            item["enrollment_id"] = key
            selected.append(item)
        if records and not selected:
            raise ValueError("no records matched the requested participant")
        copied["records"] = selected
    profile = dict(copied.get("profile") or {})
    profile["enrollment_id"] = key
    copied["profile"] = profile
    return copied, excluded


def _aggregate(participant_results: Mapping[str, Mapping[str, Any]], *, source_kind: str,
               missing_participants: Iterable[str] = ()) -> dict[str, Any]:
    grouped: dict[tuple[Any, ...], list[tuple[str, Mapping[str, Any]]]] = {}
    for key, result in participant_results.items():
        for row in result.get("summary", []):
            identity = (row.get("model_id"), row.get("source_stratum"),
                        row.get("model_version"), row.get("fusion_artifact_identity_sha256"))
            grouped.setdefault(identity, []).append((key, row))
    metrics = ("onset_mae_min", "wake_mae_min", "joint_mae_min", "interval_mae_min",
               "base_joint_mae_min", "paired_improvement_min")
    summary = []
    for identity, items in sorted(grouped.items(), key=lambda item: tuple(str(v) for v in item[0])):
        record: dict[str, Any] = {
            "model_id": identity[0], "source_stratum": identity[1],
            "model_version": identity[2], "fusion_artifact_identity_sha256": identity[3],
            "n_participants": len(items),
            "n_nights": int(sum(int(row.get("n_nights") or 0) for _, row in items)),
            "aggregation": "equal_weight_per_participant",
            "participant_keys": [key for key, _ in items],
        }
        for metric in metrics:
            values = [float(row[metric]) for _, row in items if row.get(metric) is not None]
            record[metric] = float(np.mean(values)) if values else None
            record[f"{metric}_n_participants"] = len(values)
        summary.append(record)
    missing = sorted(set(missing_participants))
    return {
        "source_kind": source_kind,
        "participant_weighting": "equal_participant_weight_for_metric_means",
        "participants_total": len(participant_results),
        "participant_keys": sorted(participant_results),
        "missing_or_unscored_participants": missing,
        "n_missing_or_unscored_participants": len(missing),
        "summary": summary,
    }


class ParticipantProspective:
    """Real prospective operations with one append-only journal per participant."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def journal_path(self, participant_id: Any) -> Path:
        return self.root / f"{participant_key(participant_id)}.sqlite3"

    def commit(self, participant_id: Any, config: Mapping[str, Any]) -> dict[str, Any]:
        raw_id = str(participant_id).strip()
        key = participant_key(raw_id)
        prepared, excluded = _filtered_records(config, raw_id, key)
        prepared.update({"mode": "prospective_commit",
                         "journal_path": str(self.journal_path(raw_id))})
        result = run_prospective(prepared)
        return {"participant_key": key, "excluded_other_participant_records": excluded,
                "source_kind": "real_prospective_journal", "result": result}

    def record_outcome(self, participant_id: Any, forecast_id: str,
                       outcome: Mapping[str, Any]) -> dict[str, Any]:
        raw_id = str(participant_id).strip()
        key = participant_key(raw_id)
        supplied = outcome.get("participant_id") or outcome.get("enrollment_id")
        if supplied not in (None, "", raw_id, key):
            raise ValueError("outcome participant does not match journal participant")
        item = dict(outcome)
        item["participant_id"] = key
        item["enrollment_id"] = key
        result = run_prospective({"mode": "prospective_outcome",
                                  "journal_path": str(self.journal_path(raw_id)),
                                  "forecast_id": forecast_id, "outcome": item})
        return {"participant_key": key, "source_kind": "real_observed_outcome",
                "result": result}

    def forecasts(self, participant_id: Any) -> list[dict[str, Any]]:
        path = self.journal_path(participant_id)
        if not path.exists():
            return []
        return list_forecasts(path)

    def summary(self, participant_id: Any | None = None) -> dict[str, Any]:
        if participant_id is not None:
            key = participant_key(participant_id)
            path = self.journal_path(participant_id)
            if not path.exists():
                return {"source_kind": "real_prospective_journal", "participants_total": 1,
                        "participant_keys": [key], "missing_or_unscored_participants": [key],
                        "n_missing_or_unscored_participants": 1, "summary": []}
            result = run_prospective({"mode": "prospective_summary", "journal_path": str(path)})
            missing = [key] if not any(int(row.get("n_nights") or 0) > 0 for row in result["summary"]) else []
            aggregate = _aggregate({key: result}, source_kind="real_prospective_journal",
                                   missing_participants=missing)
            aggregate["participant_results"] = {key: result}
            return aggregate
        results: dict[str, dict[str, Any]] = {}
        missing = []
        for path in sorted(self.root.glob("participant_*.sqlite3")):
            key = path.stem
            result = run_prospective({"mode": "prospective_summary", "journal_path": str(path)})
            results[key] = result
            if not any(int(row.get("n_nights") or 0) > 0 for row in result["summary"]):
                missing.append(key)
        aggregate = _aggregate(results, source_kind="real_prospective_journal",
                               missing_participants=missing)
        aggregate["participant_results"] = results
        return aggregate


def run_multi_participant_synthetic(csv_path: str | Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Split a declared-synthetic CSV and use the original demo per participant."""
    source = Path(csv_path).expanduser().resolve()
    fields, rows = _read_csv(source)
    if "participant_id" not in fields:
        raise ValueError("synthetic multi-participant CSV requires participant_id")
    grouped: dict[str, list[dict[str, str]]] = {}
    missing_rows = 0
    for row in rows:
        raw = str(row.get("participant_id") or "").strip()
        if not raw:
            missing_rows += 1
            continue
        grouped.setdefault(raw, []).append(row)
    if not grouped:
        raise ValueError("synthetic CSV has no identified participants")
    results: dict[str, dict[str, Any]] = {}
    missing_participants = []
    with tempfile.TemporaryDirectory(prefix="sleepwell_participants_") as temporary:
        directory = Path(temporary)
        for raw_id, participant_rows in grouped.items():
            key = participant_key(raw_id)
            path = directory / f"{key}.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for row in participant_rows:
                    item = dict(row)
                    item["participant_id"] = key
                    writer.writerow(item)
            prepared = deepcopy(dict(config))
            # The original adapter gives an in-memory ``records`` list priority
            # over ``csv_file``.  Remove it so every participant is guaranteed
            # to replay only the isolated file created above.
            prepared.pop("records", None)
            profile = dict(prepared.get("profile") or {})
            profile["enrollment_id"] = key
            prepared.update({"mode": "prospective_demo", "csv_file": str(path),
                             "input_synthetic": True, "profile": profile})
            result = run_prospective(prepared)
            results[key] = result
            if not any(int(row.get("n_nights") or 0) > 0 for row in result["summary"]):
                missing_participants.append(key)
    aggregate = _aggregate(results, source_kind="declared_synthetic_multi_participant_playback",
                           missing_participants=missing_participants)
    aggregate.update({
        "input_path": str(source),
        "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "input_rows": len(rows), "rows_missing_participant_id": missing_rows,
        "participant_row_counts": {participant_key(raw): len(values)
                                   for raw, values in grouped.items()},
        "participant_results": results,
        "warnings": [
            "Synthetic chronological playback is not genuine prospective validation.",
            "Each participant was replayed independently through the original single-participant policy.",
            "Aggregate metric means weight participants equally; night counts remain separately reported.",
        ],
    })
    json.dumps(aggregate, ensure_ascii=False, allow_nan=False)
    return aggregate
