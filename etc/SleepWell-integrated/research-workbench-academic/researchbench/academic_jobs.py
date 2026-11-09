"""Spawned academic jobs; original model code cannot block the Tk event loop."""
from __future__ import annotations

import copy
import hashlib
import math
import multiprocessing as mp
import queue
import time
import traceback
from pathlib import Path

from .academic_store import timestamp


def execute(config, project_path, progress=None, cancelled=None):
    config = copy.deepcopy(config)
    snapshot = config.pop("input_snapshot", None)
    if snapshot:
        candidate = (Path(project_path) / snapshot).resolve()
        if not candidate.is_relative_to((Path(project_path) / "inputs").resolve()):
            raise ValueError("Input snapshot escaped project")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != config.get("data_hash"):
            raise ValueError("Input snapshot SHA256 mismatch")
        config["csv_file"] = str(candidate)
    config["journal_path"] = str(Path(project_path) / "prospective.sqlite3")
    if config["mode"] in {
        "pooled_retraining", "controlled_comparison", "cross_cohort_evaluation",
        "participant_revalidation", "personalization_comparison",
    }:
        database = Path(config["database_path"]).resolve()
        if not database.is_file():
            raise FileNotFoundError(database)
        digest = hashlib.sha256()
        with database.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != config.get("data_hash"):
            raise ValueError("Retraining database SHA256 mismatch")
        config["database_path"] = str(database)
        config["input_synthetic"] = False
        if config["mode"] == "personalization_comparison":
            from .personalization_comparison import run_comparison
            result = run_comparison(config, progress=progress, cancelled=cancelled)
        elif config["mode"] == "participant_revalidation":
            audit = Path(config["participant_audit_path"]).resolve()
            if not audit.is_file():
                raise FileNotFoundError(audit)
            audit_hash = hashlib.sha256(audit.read_bytes()).hexdigest()
            if audit_hash != config.get("participant_audit_sha256"):
                raise ValueError("Participant audit SHA256 mismatch")
            config["participant_audit_path"] = str(audit)
            if config.get("untouched_holdout_path"):
                holdout = Path(config["untouched_holdout_path"]).resolve()
                if not holdout.is_file():
                    raise FileNotFoundError(holdout)
                holdout_hash = hashlib.sha256(holdout.read_bytes()).hexdigest()
                if holdout_hash != config.get("untouched_holdout_sha256"):
                    raise ValueError("Untouched holdout SHA256 mismatch")
                config["untouched_holdout_path"] = str(holdout)
            from .participant_revalidation import run_participant_revalidation
            result = run_participant_revalidation(config, progress=progress, cancelled=cancelled)
        elif config["mode"] == "cross_cohort_evaluation":
            source_run = Path(config["source_run_dir"]).resolve()
            if not source_run.is_dir():
                raise FileNotFoundError(source_run)
            config["source_run_dir"] = str(source_run)
            from .cross_evaluation import run_cross_evaluation
            result = run_cross_evaluation(config, progress=progress, cancelled=cancelled)
        elif config["mode"] == "controlled_comparison":
            from .controlled_comparison import run_controlled_comparison
            result = run_controlled_comparison(config, progress=progress, cancelled=cancelled)
        else:
            from .pooled_training import run_pooled_retraining
            result = run_pooled_retraining(config, progress=progress, cancelled=cancelled)
    elif config["mode"].startswith("prospective_"):
        from .academic_prospective import run_prospective
        result = run_prospective(config, progress=progress, cancelled=cancelled)
    else:
        from .academic_backend import run_research
        result = run_research(config, progress=progress, cancelled=cancelled)
    result.setdefault("provenance", {})["input_sha256"] = config.get("data_hash")
    result["provenance"]["input_source"] = config.get("source_name", "generated synthetic")
    result["provenance"]["input_synthetic"] = config.get("input_synthetic", False)
    return result


def _worker(config, path, messages, stop):
    try:
        result = execute(config, path, progress=lambda e: messages.put(("progress", e)), cancelled=stop.is_set)
        messages.put(("result", result))
    except BaseException as exc:
        messages.put(("error", {"error": str(exc), "traceback": traceback.format_exc()}))


class AcademicQueue:
    def __init__(self, project, changed=None):
        self.project = project
        self.changed = changed or (lambda record: None)
        self.context = mp.get_context("spawn")
        self.pending = []
        self.active = None

    @property
    def busy(self):
        return bool(self.active or self.pending)

    def _save(self, record):
        self.project.save_run(record)
        self.changed(record)

    def submit(self, config):
        raw_budget = config.get("max_seconds", 600)
        if isinstance(raw_budget, bool):
            raise ValueError("Run budget must be a number, not a boolean")
        budget = float(raw_budget)
        if not math.isfinite(budget) or not 1 <= budget <= 86400:
            raise ValueError("Run budget must be between 1 and 86400 seconds")
        record = self.project.new_record(config)
        self._save(record)
        self.pending.append(record)
        return record["id"]

    def poll(self):
        if self.active:
            active = self.active
            record = active["record"]
            record["duration"] = time.monotonic() - active["started"]
            # Drain while alive too: a large result must never deadlock its feeder.
            for _ in range(100):
                try:
                    kind, payload = active["messages"].get_nowait()
                except queue.Empty:
                    break
                if kind == "progress":
                    record["progress"] = payload
                    record["stage"] = payload.get("stage", "running") if isinstance(payload, dict) else str(payload)
                    self.changed(record)
                else:
                    active["terminal"] = (kind, payload)
            if record["duration"] > float(record["config"].get("max_seconds", 600)) and not active.get("cancelled_at"):
                self.cancel(record["id"], "time_budget")
            if active.get("cancelled_at") and active["process"].is_alive() and time.monotonic() - active["cancelled_at"] > 1:
                active["process"].terminate()
            if not active["process"].is_alive():
                active["process"].join(timeout=0)
                while True:
                    try:
                        kind, payload = active["messages"].get_nowait()
                        if kind != "progress": active["terminal"] = (kind, payload)
                    except queue.Empty:
                        break
                if active.get("cancelled_at"):
                    record.update(status="cancelled", stage="cancelled")
                elif active.get("terminal"):
                    kind, payload = active["terminal"]
                    if kind == "result":
                        record.update(status="completed", stage="completed", result=payload)
                    else:
                        record.update(status="failed", stage="failed", **payload)
                else:
                    record.update(status="failed", stage="failed", error=f"Worker exit {active['process'].exitcode}")
                record["finished"] = timestamp()
                active["messages"].close()
                self._save(record)
                self.active = None
        if not self.active and self.pending:
            record = self.pending.pop(0)
            messages, stop = self.context.Queue(), self.context.Event()
            process = self.context.Process(target=_worker, args=(record["config"], str(self.project.path), messages, stop), daemon=True)
            record.update(status="running", stage="starting")
            self._save(record)
            try:
                process.start()
                self.active = {"record": record, "messages": messages, "stop": stop,
                               "process": process, "started": time.monotonic(), "terminal": None}
            except Exception as exc:
                record.update(status="failed", stage="failed", error=str(exc), traceback=traceback.format_exc())
                self._save(record)
                messages.close()

    def cancel(self, run_id, reason="user_cancel"):
        if self.active and self.active["record"]["id"] == run_id:
            active = self.active
            active["stop"].set()
            active.setdefault("cancelled_at", time.monotonic())
            active["record"].update(status="cancelling", stage="cancelling", cancel_reason=reason)
            self._save(active["record"])
        else:
            for record in list(self.pending):
                if record["id"] == run_id:
                    record.update(status="cancelled", stage="cancelled", finished=timestamp())
                    self.pending.remove(record)
                    self._save(record)

    def close(self):
        for record in list(self.pending): self.cancel(record["id"])
        if self.active:
            active = self.active
            self.cancel(active["record"]["id"])
            active["process"].join(timeout=.2)
            if active["process"].is_alive():
                active["process"].terminate()
                active["process"].join(timeout=3)
            self.poll()
