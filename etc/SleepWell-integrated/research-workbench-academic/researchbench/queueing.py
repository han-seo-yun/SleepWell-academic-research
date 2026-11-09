"""Single-process queue. All Tk and project writes remain in the parent process."""
from __future__ import annotations

import copy
import multiprocessing as mp
import queue
import time
import traceback
import uuid


def _worker(df, config, plan, out, stop, final):
    from .engine import evaluate_final, run_experiment
    try:
        runner = evaluate_final if final else run_experiment
        result = runner(df, config, plan, progress=lambda e: out.put(("progress", e)),
                        cancelled=stop.is_set)
        out.put(("result", result))
    except BaseException as exc:
        out.put(("error", {"error": str(exc), "error_type": type(exc).__name__,
                            "traceback": traceback.format_exc()}))


class ExperimentQueue:
    """Call poll regularly. Cancellation is complete only after process exit."""
    def __init__(self, project, on_change=None):
        self.project = project
        self.on_change = on_change or (lambda record: None)
        self.ctx = mp.get_context("spawn")
        self.pending = []
        self.active = None
        self.records = {}

    def submit(self, config, plan, final=False):
        from .engine import resolve_config
        config = resolve_config(self.project.df, config, plan)
        config["id"] = str(uuid.uuid4())
        record = {"id": config["id"], "name": config["name"], "task": config["task"],
                  "config": config, "status": "queued", "stage": "queued",
                  "phase": "final_test" if final else "validation", "data_hash": config["data_hash"],
                  "split_id": plan["id"], "duration": 0, "created": time.time()}
        self.records[record["id"]] = record
        self.pending.append((record, copy.deepcopy(plan), final))
        self._save(record)
        return record["id"]

    def _save(self, record):
        self.project.save_experiment(copy.deepcopy(record))
        self.on_change(record)

    def poll(self):
        if self.active:
            a = self.active
            record = a["record"]
            record["duration"] = time.monotonic() - a["started"]
            for _ in range(100):
                try:
                    kind, value = a["out"].get_nowait()
                except queue.Empty:
                    break
                if kind == "progress":
                    record["progress"] = value
                    record["stage"] = value.get("stage", "training")
                    if "epoch" in value:
                        record.setdefault("history", []).append(copy.deepcopy(value))
                    self.on_change(record)
                else:
                    a["terminal"] = (kind, value)
            limit = float(record["config"].get("max_seconds", 3600))
            if record["duration"] > limit and not a.get("cancel_at"):
                self.cancel(record["id"], reason="time_budget")
            if a.get("cancel_at") and a["process"].is_alive() and time.monotonic() - a["cancel_at"] > 1.0:
                a["process"].terminate()
            if not a["process"].is_alive():
                a["process"].join(timeout=0)
                # Drain after process exit: feeder may deliver the final event late.
                while True:
                    try:
                        kind, value = a["out"].get_nowait()
                        if kind != "progress":
                            a["terminal"] = (kind, value)
                    except queue.Empty:
                        break
                if a.get("cancel_at"):
                    record.update(status="cancelled", stage="cancelled")
                elif a.get("terminal"):
                    kind, value = a["terminal"]
                    if kind == "result":
                        record.update(value)
                        record.update(id=record["config"]["id"], status="completed", stage="completed")
                    else:
                        record.update(value)
                        record.update(status="failed", stage="failed")
                else:
                    record.update(status="failed", stage="failed", error="worker_exit",
                                  error_type="WorkerExit", traceback=f"Exit code: {a['process'].exitcode}")
                a["out"].close()
                self._save(record)
                self.active = None
        if not self.active and self.pending:
            record, plan, final = self.pending.pop(0)
            out = self.ctx.Queue()
            stop = self.ctx.Event()
            process = self.ctx.Process(target=_worker, args=(self.project.df, record["config"], plan, out, stop, final), daemon=True)
            record.update(status="running", stage="starting")
            self._save(record)
            try:
                process.start()
                self.active = {"record": record, "process": process, "out": out, "stop": stop,
                               "started": time.monotonic(), "terminal": None}
            except Exception as exc:
                record.update(status="failed", error=str(exc), traceback=traceback.format_exc())
                self._save(record)
                out.close()

    def cancel(self, experiment_id, reason="user_cancel"):
        if self.active and self.active["record"]["id"] == experiment_id:
            a = self.active
            a["stop"].set()
            a["cancel_at"] = a.get("cancel_at", time.monotonic())
            a["record"].update(status="cancelling", stage="cancelling", cancel_reason=reason)
            self._save(a["record"])
        else:
            for item in list(self.pending):
                if item[0]["id"] == experiment_id:
                    self.pending.remove(item)
                    item[0].update(status="cancelled", stage="cancelled", cancel_reason=reason)
                    self._save(item[0])

    def close(self):
        for item in list(self.pending):
            self.cancel(item[0]["id"])
        if self.active:
            a = self.active
            self.cancel(a["record"]["id"])
            a["process"].join(timeout=0.3)
            if a["process"].is_alive():
                a["process"].terminate()
                a["process"].join(timeout=2)
            self.poll()

    @property
    def busy(self):
        return bool(self.active or self.pending)
