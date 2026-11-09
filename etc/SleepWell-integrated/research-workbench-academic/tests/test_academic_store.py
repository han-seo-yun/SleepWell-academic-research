from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from researchbench import academic_store
from researchbench.academic_store import AcademicProject


class AcademicStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "project.rwa"
        self.project = AcademicProject.create(self.path, "QA project")

    def tearDown(self):
        self.temp.cleanup()

    def record(self, status="queued"):
        value = self.project.new_record({"mode": "prospective_demo", "seed": 42})
        value["status"] = status
        value["stage"] = status
        if status == "completed":
            value["result"] = {"mode": "prospective_demo", "summary": []}
        return value

    def test_completed_and_other_terminal_records_are_immutable(self):
        for status in ("completed", "failed", "cancelled", "interrupted"):
            with self.subTest(status=status):
                record = self.record(status)
                self.project.save_run(record)
                self.project.save_run(copy.deepcopy(record))
                changed = copy.deepcopy(record)
                changed["stage"] = "tampered"
                with self.assertRaisesRegex(ValueError, "finished academic run is immutable"):
                    self.project.save_run(changed)

    def test_saved_completed_result_is_detached_from_caller_mutation(self):
        record = self.record("completed")
        self.project.save_run(record)
        record["result"]["summary"].append({"model_id": "tampered"})
        stored = next(row for row in self.project.runs() if row["id"] == record["id"])
        self.assertEqual(stored["result"]["summary"], [])

    def test_terminal_run_hash_detects_direct_disk_tampering(self):
        record = self.record("completed")
        self.project.save_run(record)
        self.assertIn(record["id"], self.project.state["run_hashes"])
        path = self.path / "runs" / f"{record['id']}.json"
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["result"]["summary"] = [{"model_id": "forged"}]
        path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "failed SHA256 verification"):
            self.project.runs()
        with self.assertRaisesRegex(ValueError, "failed SHA256 verification"):
            AcademicProject.open(self.path)

    def test_legacy_project_without_hashes_remains_openable(self):
        record = self.record("completed")
        self.project.save_run(record)
        state_path = self.path / "project.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.pop("run_hashes")
        academic_store.write_json(state_path, state)
        reopened = AcademicProject.open(self.path)
        self.assertEqual(reopened.runs()[0]["id"], record["id"])

    def test_terminal_state_hash_is_not_published_when_project_save_fails(self):
        record = self.record("running")
        self.project.save_run(record)
        record.update(status="completed", stage="completed", result={"summary": []})
        original = academic_store.write_json

        def fail_project(path, value):
            if Path(path).name == "project.json":
                raise OSError("simulated project save failure")
            return original(path, value)

        with mock.patch.object(academic_store, "write_json", side_effect=fail_project):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.project.save_run(record)
        self.assertNotIn(record["id"], self.project.state.get("run_hashes", {}))
        # The completed run exists, but no unsaved digest creates a false mismatch.
        self.assertEqual(self.project.runs()[0]["status"], "completed")

    def test_submitted_configuration_is_immutable_before_completion(self):
        record = self.record("running")
        self.project.save_run(record)
        changed = copy.deepcopy(record)
        changed["config"]["seed"] = 99
        with self.assertRaisesRegex(ValueError, "configuration is immutable"):
            self.project.save_run(changed)

    def test_open_recovers_active_states_as_interrupted_and_keeps_completed(self):
        identifiers = {}
        for status in ("queued", "running", "cancelling", "completed"):
            record = self.record(status)
            identifiers[status] = record["id"]
            self.project.save_run(record)
        reopened = AcademicProject.open(self.path)
        rows = {row["id"]: row for row in reopened.runs()}
        for status in ("queued", "running", "cancelling"):
            recovered = rows[identifiers[status]]
            self.assertEqual(recovered["status"], "interrupted")
            self.assertIn("finished", recovered)
        self.assertEqual(rows[identifiers["completed"]]["status"], "completed")
        # Recovery is terminal and stable on subsequent opens.
        first = json.dumps(rows, sort_keys=True)
        second = {row["id"]: row for row in AcademicProject.open(self.path).runs()}
        self.assertEqual(json.dumps(second, sort_keys=True), first)

    def test_input_snapshot_is_content_addressed_and_path_confined(self):
        source = Path(self.temp.name) / "input.csv"
        source.write_text("cutoff,value\n2026-01-01,1\n", encoding="utf-8")
        imported = self.project.import_data(source, synthetic=True)
        snapshot = self.project.snapshot_path(imported["snapshot"])
        self.assertTrue(snapshot.is_file())
        self.assertTrue(imported["synthetic"])
        with self.assertRaisesRegex(ValueError, "stay inside"):
            self.project.snapshot_path("../project.json")

    def test_corrupted_existing_snapshot_is_rejected_without_overwrite(self):
        source = Path(self.temp.name) / "input.csv"
        source.write_text("x\n1\n", encoding="utf-8")
        imported = self.project.import_data(source)
        snapshot = self.project.snapshot_path(imported["snapshot"])
        snapshot.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "failed SHA256 verification"):
            self.project.import_data(source)
        self.assertEqual(snapshot.read_bytes(), b"corrupt")

    def test_project_and_run_files_reject_nonfinite_json(self):
        record = self.record()
        record["duration"] = float("nan")
        with self.assertRaises(ValueError):
            self.project.save_run(record)
        self.assertFalse((self.path / "runs" / f"{record['id']}.json").exists())


if __name__ == "__main__":
    unittest.main()
