"""Atomic, JSON-based project persistence without executable serialization."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .data import fingerprint


FORMAT_VERSION = 1
ACTIVE_STATUSES = {"queued", "running", "cancelling"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if pd.isna(value):
        return None
    raise TypeError(f"not_json_serializable:{type(value).__name__}")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _write_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, default=_json_default, allow_nan=False))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot_value(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and np.isnan(value):
        return None
    if isinstance(value, float) and np.isinf(value):
        raise ValueError("nonfinite_data_not_supported")
    return value


def _snapshot_text(frame: pd.DataFrame) -> str:
    """Create pandas table JSON while retaining Python's round-trip float repr."""
    skeleton = json.loads(
        frame.iloc[:0].to_json(orient="table", index=False, date_format="iso", force_ascii=False)
    )
    skeleton["data"] = [
        {str(column): _snapshot_value(value) for column, value in zip(frame.columns, row)}
        for row in frame.itertuples(index=False, name=None)
    ]
    return json.dumps(skeleton, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Project:
    def __init__(self, path: Path, df: pd.DataFrame, state: dict[str, Any]):
        self.path = path
        self.df = df.reset_index(drop=True)
        self.state = state
        self._closed = False

    @classmethod
    def create(
        cls, path: str | Path, df: pd.DataFrame, name: str, source_path: str = ""
    ) -> "Project":
        nonfinite: list[str] = []
        for column in df.select_dtypes(include=np.number).columns:
            count = int(np.isinf(pd.to_numeric(df[column], errors="coerce").to_numpy(dtype=float, na_value=np.nan)).sum())
            if count:
                nonfinite.append(f"{column} ({count})")
        if nonfinite:
            raise ValueError("nonfinite_values:" + ", ".join(nonfinite))
        root = Path(path).expanduser().resolve()
        if root.exists() and any(root.iterdir()):
            raise FileExistsError(f"project_directory_not_empty:{root}")
        root.mkdir(parents=True, exist_ok=True)
        frame = df.reset_index(drop=True).copy()
        state = {
            "format_version": FORMAT_VERSION,
            "name": name,
            "data_hash": fingerprint(frame),
            "source_path": str(Path(source_path).expanduser().resolve()) if source_path else "",
            "source_changed": False,
            "roles": {},
            "tasks": {},
            "baselines": {"classification": None, "regression": None},
            "graph_settings": {},
            "ui": {"language": "ko"},
            "splits": {},
            "created_at": datetime.now().astimezone().isoformat(),
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        project = cls(root, frame, state)
        _atomic_text(project.path / "data.json", _snapshot_text(project.df))
        project.save()
        return project

    @classmethod
    def open(cls, path: str | Path) -> "Project":
        root = Path(path).expanduser().resolve()
        state_path = root / "project.json"
        snapshot_path = root / "data.json"
        if not state_path.is_file() or not snapshot_path.is_file():
            raise FileNotFoundError(f"invalid_project:{root}")
        state = _read_json(state_path)
        if state.get("format_version") != FORMAT_VERSION:
            raise ValueError(f"unsupported_project_version:{state.get('format_version')}")
        frame = pd.read_json(snapshot_path, orient="table", precise_float=True)
        frame = frame.reset_index(drop=True)
        stored_hash = state.get("data_hash")
        actual_hash = fingerprint(frame)
        if stored_hash != actual_hash:
            raise ValueError("project_snapshot_hash_mismatch")
        project = cls(root, frame, state)
        changed = False
        source = Path(state["source_path"]) if state.get("source_path") else None
        if source and source.is_file():
            try:
                from .data import load_data

                state["source_changed"] = fingerprint(load_data(source)) != stored_hash
            except Exception:
                state["source_changed"] = True
            changed = True
        for record_path in (root / "experiments").glob("*.json") if (root / "experiments").exists() else []:
            record = _read_json(record_path)
            if record.get("status") in ACTIVE_STATUSES:
                record["status"] = "interrupted"
                record["interrupted_reason"] = "application_ended_before_completion"
                _write_json(record_path, record)
                changed = True
        if changed:
            project.save()
        return project

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("project_is_closed")

    def save(self) -> None:
        self._ensure_open()
        current_hash = fingerprint(self.df)
        if current_hash != self.state.get("data_hash"):
            raise ValueError("new_data_version_required")
        if not (self.path / "data.json").is_file():
            raise ValueError("project_snapshot_missing")
        self.state["updated_at"] = datetime.now().astimezone().isoformat()
        _write_json(self.path / "project.json", self.state)

    def save_experiment(self, record: dict[str, Any]) -> None:
        self._ensure_open()
        experiment_id = str(record.get("id", "")).strip()
        if not experiment_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in experiment_id):
            raise ValueError("invalid_experiment_id")
        target = self.path / "experiments" / f"{experiment_id}.json"
        incoming = deepcopy(record)
        if target.exists():
            existing = _read_json(target)
            if existing.get("status") in TERMINAL_STATUSES:
                canonical_existing = json.dumps(existing, ensure_ascii=False, sort_keys=True, default=_json_default)
                canonical_incoming = json.dumps(incoming, ensure_ascii=False, sort_keys=True, default=_json_default)
                if canonical_existing == canonical_incoming:
                    return
                raise ValueError("terminal_experiment_is_immutable")
            if existing.get("config") != incoming.get("config"):
                raise ValueError("experiment_config_is_immutable")
        _write_json(target, incoming)

    def experiments(self) -> list[dict[str, Any]]:
        self._ensure_open()
        directory = self.path / "experiments"
        if not directory.exists():
            return []
        records = [_read_json(path) for path in directory.glob("*.json")]
        return sorted(records, key=lambda record: (str(record.get("created_at", "")), str(record.get("id", ""))))

    def close(self) -> None:
        if not self._closed:
            self.save()
            self._closed = True

    def __enter__(self) -> "Project":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

