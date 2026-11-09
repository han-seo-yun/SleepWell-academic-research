"""Portable academic projects with immutable input snapshots and completed runs."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _record_sha256(record):
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AcademicProject:
    def __init__(self, path, state):
        self.path = Path(path).resolve()
        self.state = state

    @classmethod
    def create(cls, path, name="SleepWell Academic"):
        path = Path(path).resolve()
        if path.exists() and any(path.iterdir()):
            raise FileExistsError(f"Project directory is not empty: {path}")
        path.mkdir(parents=True, exist_ok=True)
        (path / "runs").mkdir()
        (path / "inputs").mkdir()
        project = cls(path, {"schema": 1, "name": name, "created": timestamp(),
                             "input": None, "ui": {}, "run_ids": [], "run_hashes": {}})
        project.save()
        return project

    @classmethod
    def open(cls, path):
        path = Path(path).resolve()
        state = json.loads((path / "project.json").read_text(encoding="utf-8"))
        if state.get("schema") != 1 or "run_ids" not in state:
            raise ValueError("Not an academic project")
        project = cls(path, state)
        for record in project.runs():
            if record["status"] in {"queued", "running", "cancelling"}:
                record.update(status="interrupted", finished=timestamp())
                project.save_run(record)
        return project

    def save(self):
        self.state["saved"] = timestamp()
        write_json(self.path / "project.json", self.state)

    def import_data(self, path, *, synthetic=False):
        source = Path(path).resolve()
        original = source.read_bytes()
        source_hash = hashlib.sha256(original).hexdigest()
        if source.suffix.lower() in {".xlsx", ".xlsm"}:
            import pandas as pd
            data = pd.read_excel(source).to_csv(index=False).encode("utf-8-sig")
        elif source.suffix.lower() == ".json":
            value = json.loads(original.decode("utf-8-sig"))
            records = value.get("records") if isinstance(value, dict) else value
            if not isinstance(records, list):
                raise ValueError("JSON must contain an array of records")
            import pandas as pd
            data = pd.DataFrame(records).to_csv(index=False).encode("utf-8-sig")
        elif source.suffix.lower() == ".csv":
            data = original
        else:
            raise ValueError("CSV, Excel or JSON records are required")
        relative, digest = self.store_input_snapshot(data)
        self.state["input"] = {"snapshot": relative, "sha256": digest,
                               "source_sha256": source_hash, "source_name": source.name,
                               "synthetic": bool(synthetic), "imported": timestamp()}
        self.save()
        return self.state["input"]

    def store_input_snapshot(self, data: bytes) -> tuple[str, str]:
        """Store a complete immutable input without changing the active dataset."""
        digest = hashlib.sha256(data).hexdigest()
        relative = f"inputs/{digest}.csv"
        destination = self.path / relative
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ValueError("Existing input snapshot failed SHA256 verification")
        else:
            fd, name = tempfile.mkstemp(prefix='.input-', suffix='.tmp', dir=destination.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        return relative, digest

    def snapshot_path(self, relative):
        candidate = (self.path / relative).resolve()
        if not candidate.is_relative_to(self.path / "inputs"):
            raise ValueError("Input snapshot must stay inside this project")
        return candidate

    def _run_path(self, run_id):
        uuid.UUID(str(run_id))
        return self.path / "runs" / f"{run_id}.json"

    def save_run(self, record):
        path = self._run_path(record["id"])
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous["status"] in TERMINAL and previous != record:
                raise ValueError("A finished academic run is immutable; create a new run")
            if previous["config"] != record["config"]:
                raise ValueError("A submitted run configuration is immutable")
        write_json(path, record)
        next_state = copy.deepcopy(self.state)
        state_changed = False
        if record["id"] not in next_state["run_ids"]:
            next_state["run_ids"].append(record["id"])
            state_changed = True
        if record["status"] in TERMINAL:
            run_hashes = next_state.setdefault("run_hashes", {})
            digest = _record_sha256(record)
            if run_hashes.get(record["id"]) != digest:
                run_hashes[record["id"]] = digest
                state_changed = True
        if state_changed:
            next_state["saved"] = timestamp()
            write_json(self.path / "project.json", next_state)
            self.state = next_state

    def runs(self):
        records = [json.loads(path.read_text(encoding="utf-8"))
                   for path in sorted((self.path / "runs").glob("*.json"))]
        hashes = self.state.get("run_hashes") or {}
        for record in records:
            expected = hashes.get(record.get("id"))
            if expected is not None and expected != _record_sha256(record):
                raise ValueError(f"Academic run failed SHA256 verification: {record.get('id')}")
        return records

    def new_record(self, config):
        return {"id": str(uuid.uuid4()), "name": config.get("name", config["mode"]),
                "config": copy.deepcopy(config), "created": timestamp(),
                "status": "queued", "duration": 0.0, "stage": "queued"}
