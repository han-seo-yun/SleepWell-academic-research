from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from researchbench.academic_jobs import AcademicQueue, execute
from researchbench.academic_store import AcademicProject


SYNTHETIC_50 = (Path(__file__).resolve().parents[2] / "work" / "demo-personal-50d" /
                "SYNTHETIC_50일_개인화시연" / "SYNTHETIC_개인수면_50일.csv")


class AcademicJobIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = AcademicProject.create(Path(self.temp.name) / "academic.rwa")

    def tearDown(self):
        self.temp.cleanup()

    def imported_config(self):
        item = self.project.import_data(SYNTHETIC_50, synthetic=True)
        return {
            "mode": "prospective_demo",
            "name": "50-night prospective playback",
            "input_snapshot": item["snapshot"],
            "data_hash": item["sha256"],
            "source_name": item["source_name"],
            "input_synthetic": item["synthetic"],
        }

    def test_import_snapshot_execute_and_persist_reopen_preserve_provenance(self):
        config = self.imported_config()
        result = execute(config, self.project.path)
        self.assertEqual(result["mode"], "prospective_demo")
        self.assertEqual(len(result["raw"]["forecasts"]), 50)
        self.assertEqual(result["raw"]["aggregate"]["evaluated_nights"], 50)
        self.assertEqual(result["provenance"]["input_sha256"], config["data_hash"])
        self.assertTrue(result["provenance"]["input_synthetic"])
        record = self.project.new_record(config)
        record.update(status="completed", stage="completed", result=result)
        self.project.save_run(record)
        reopened = AcademicProject.open(self.project.path)
        restored = next(row for row in reopened.runs() if row["id"] == record["id"])
        self.assertEqual(restored["result"]["raw"]["aggregate"]["evaluated_nights"], 50)
        self.assertEqual(restored["result"]["provenance"]["input_sha256"],
                         config["data_hash"])

    def test_snapshot_hash_mismatch_is_rejected_before_model_execution(self):
        config = self.imported_config()
        snapshot = self.project.snapshot_path(config["input_snapshot"])
        snapshot.write_bytes(snapshot.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            execute(config, self.project.path)

    def test_snapshot_path_escape_is_rejected_before_reading_external_data(self):
        outside = Path(self.temp.name) / "outside.csv"
        outside.write_text("participant_id,cutoff\nx,2026-01-01T20:00:00+09:00\n",
                           encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "escaped project"):
            execute({
                "mode": "prospective_demo", "input_snapshot": "../outside.csv",
                "data_hash": "irrelevant", "input_synthetic": True,
            }, self.project.path)

    def test_spawned_queue_completes_and_saves_json_safe_prospective_result(self):
        queue = AcademicQueue(self.project)
        run_id = queue.submit({
            "mode": "prospective_demo", "name": "short playback",
            "n_nights": 3, "seed": 7, "max_seconds": 60,
            "input_synthetic": True,
        })
        deadline = time.monotonic() + 30
        try:
            while queue.busy and time.monotonic() < deadline:
                queue.poll()
                time.sleep(0.02)
            self.assertFalse(queue.busy, "spawned academic job did not finish")
        finally:
            queue.close()
        record = next(row for row in self.project.runs() if row["id"] == run_id)
        self.assertEqual(record["status"], "completed", record.get("traceback"))
        self.assertEqual(record["result"]["raw"]["aggregate"]["evaluated_nights"], 3)
        self.assertEqual(len(record["result"]["summary"]), 13)
        json.dumps(record, ensure_ascii=False, allow_nan=False)

    def test_active_cancel_stops_the_spawned_process_before_terminal_save(self):
        queue = AcademicQueue(self.project)
        run_id = queue.submit({
            "mode": "prospective_demo", "name": "cancel playback",
            "n_nights": 500, "seed": 7, "max_seconds": 60,
            "input_synthetic": True,
        })
        queue.poll()
        self.assertIsNotNone(queue.active)
        process = queue.active["process"]
        queue.cancel(run_id)
        deadline = time.monotonic() + 10
        try:
            while queue.busy and time.monotonic() < deadline:
                queue.poll()
                time.sleep(0.02)
            self.assertFalse(queue.busy, "cancelled process did not reach a terminal state")
            self.assertFalse(process.is_alive(), "cancelled worker process is still alive")
        finally:
            queue.close()
        record = next(row for row in self.project.runs() if row["id"] == run_id)
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["cancel_reason"], "user_cancel")
        self.assertIn("finished", record)

    def test_elapsed_budget_cancels_and_stops_spawned_process(self):
        queue = AcademicQueue(self.project)
        run_id = queue.submit({
            "mode": "prospective_demo", "name": "budget playback",
            "n_nights": 500, "seed": 11, "max_seconds": 1,
            "input_synthetic": True,
        })
        queue.poll()
        process = queue.active["process"]
        deadline = time.monotonic() + 12
        try:
            while queue.busy and time.monotonic() < deadline:
                queue.poll()
                time.sleep(0.03)
            self.assertFalse(queue.busy, "budgeted process did not reach a terminal state")
            self.assertFalse(process.is_alive(), "budget-cancelled worker is still alive")
        finally:
            queue.close()
        record = next(row for row in self.project.runs() if row["id"] == run_id)
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["cancel_reason"], "time_budget")
        self.assertGreaterEqual(record["duration"], 1)

    def test_budget_validation_rejects_bounds_and_nonfinite_values_without_artifacts(self):
        queue = AcademicQueue(self.project)
        for value in (0, -1, 86401, float("inf"), float("nan"), True, False):
            with self.subTest(value=value):
                before = list((self.project.path / "runs").iterdir())
                with self.assertRaises(ValueError):
                    queue.submit({"mode": "prospective_demo", "max_seconds": value})
                self.assertEqual(list((self.project.path / "runs").iterdir()), before)
        self.assertFalse(queue.busy)

    def test_budget_boundary_values_are_accepted_and_pending_runs_can_be_cancelled(self):
        queue = AcademicQueue(self.project)
        identifiers = [queue.submit({"mode": "prospective_demo", "max_seconds": value})
                       for value in (1, 86400)]
        for identity in identifiers:
            queue.cancel(identity)
        self.assertFalse(queue.busy)
        records = {row["id"]: row for row in self.project.runs()}
        self.assertEqual({records[identity]["status"] for identity in identifiers}, {"cancelled"})


if __name__ == "__main__":
    unittest.main()
