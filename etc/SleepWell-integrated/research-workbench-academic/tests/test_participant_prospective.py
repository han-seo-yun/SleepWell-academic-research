from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchbench.participant_prospective import (
    ParticipantProspective,
    participant_key,
    run_multi_participant_synthetic,
)


class ParticipantJournalTests(unittest.TestCase):
    def test_two_participants_use_isolated_hashed_journals_and_filtered_records(self):
        calls = []

        def fake_run(config):
            calls.append(config)
            return {"summary": [], "raw": {}, "mode": config["mode"]}

        with tempfile.TemporaryDirectory() as temporary, patch(
            "researchbench.participant_prospective.run_prospective", side_effect=fake_run
        ):
            wrapper = ParticipantProspective(temporary)
            records = [
                {"participant_id": "alice", "sleep_start": "a"},
                {"participant_id": "bob", "sleep_start": "b"},
            ]
            alice = wrapper.commit("alice", {"records": records})
            bob = wrapper.commit("bob", {"records": records})
            self.assertNotEqual(calls[0]["journal_path"], calls[1]["journal_path"])
            self.assertNotIn("alice", calls[0]["journal_path"])
            self.assertNotIn("bob", calls[1]["journal_path"])
            self.assertEqual(calls[0]["records"][0]["participant_id"], participant_key("alice"))
            self.assertEqual(calls[1]["records"][0]["participant_id"], participant_key("bob"))
            self.assertEqual(alice["excluded_other_participant_records"], 1)
            self.assertEqual(bob["excluded_other_participant_records"], 1)

    def test_outcome_rejects_cross_participant_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            wrapper = ParticipantProspective(temporary)
            with self.assertRaisesRegex(ValueError, "does not match"):
                wrapper.record_outcome("alice", "forecast-1", {"participant_id": "bob"})

    def test_all_journal_summary_weights_participants_equally(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (participant_key("a"), participant_key("b")):
                (root / f"{name}.sqlite3").touch()

            def fake_run(config):
                value = 10.0 if Path(config["journal_path"]).stem == participant_key("a") else 30.0
                nights = 2 if value == 10 else 20
                return {"summary": [{"model_id": "fusion", "source_stratum": "diary",
                                     "model_version": "v1", "joint_mae_min": value,
                                     "n_nights": nights}], "raw": {}, "mode": "prospective_summary"}

            with patch("researchbench.participant_prospective.run_prospective", side_effect=fake_run):
                result = ParticipantProspective(root).summary()
            self.assertEqual(result["participants_total"], 2)
            self.assertEqual(result["summary"][0]["joint_mae_min"], 20.0)
            self.assertEqual(result["summary"][0]["n_nights"], 22)
            self.assertEqual(result["summary"][0]["aggregation"], "equal_weight_per_participant")


class SyntheticMultiParticipantTests(unittest.TestCase):
    def test_actual_adapter_runs_each_synthetic_participant_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "actual_multi.csv"
            fields = ["participant_id", "cutoff", "sleep_start", "sleep_end", "observed_at", "source"]
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for participant in ("actual-a", "actual-b"):
                    writer.writerow({"participant_id": participant,
                                     "cutoff": "2026-01-01T20:00:00+09:00",
                                     "sleep_start": "2026-01-01T23:00:00+09:00",
                                     "sleep_end": "2026-01-02T07:00:00+09:00",
                                     "observed_at": "2026-01-02T08:00:00+09:00",
                                     "source": "diary"})
            result = run_multi_participant_synthetic(source, {"seed": 5})
            self.assertEqual(result["participants_total"], 2)
            self.assertEqual(len(result["participant_results"]), 2)
            self.assertTrue(all(item["mode"] == "prospective_demo"
                                for item in result["participant_results"].values()))

    def test_in_memory_records_cannot_override_participant_split_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "participants.csv"
            fields = ["participant_id", "cutoff", "sleep_start", "sleep_end", "observed_at", "source"]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for participant in ("p1", "p2"):
                    writer.writerow({"participant_id": participant,
                                     "cutoff": "2026-01-01T20:00:00+09:00",
                                     "sleep_start": "2026-01-01T23:00:00+09:00",
                                     "sleep_end": "2026-01-02T07:00:00+09:00",
                                     "observed_at": "2026-01-02T08:00:00+09:00",
                                     "source": "diary"})
            config = {"seed": 9}
            config["records"] = [{"participant_id": "cross-contaminating-record"}]
            seen_paths = []

            def fake_run(supplied):
                self.assertNotIn("records", supplied)
                seen_paths.append(supplied["csv_file"])
                return {"summary": [], "mode": "prospective_demo"}

            with patch("researchbench.participant_prospective.run_prospective", side_effect=fake_run) as run:
                run_multi_participant_synthetic(path, config)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(len(set(seen_paths)), 2)

    def test_split_runs_original_policy_per_person_and_aggregates_equally(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "multi.csv"
            fields = ["participant_id", "cutoff", "sleep_start", "sleep_end", "observed_at", "source"]
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for participant in ("p1", "p2"):
                    for day in (1, 2):
                        writer.writerow({"participant_id": participant,
                                         "cutoff": f"2026-01-0{day}T20:00:00+09:00",
                                         "sleep_start": f"2026-01-0{day}T23:00:00+09:00",
                                         "sleep_end": f"2026-01-0{day + 1}T07:00:00+09:00",
                                         "observed_at": f"2026-01-0{day + 1}T08:00:00+09:00",
                                         "source": "diary"})
                writer.writerow({"participant_id": "", "cutoff": "2026-01-01T20:00:00+09:00"})
            seen = []

            def fake_run(config):
                with Path(config["csv_file"]).open("r", encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                ids = {row["participant_id"] for row in rows}
                self.assertEqual(len(ids), 1)
                self.assertTrue(next(iter(ids)).startswith("participant_"))
                seen.append(next(iter(ids)))
                value = 10.0 if len(seen) == 1 else 30.0
                return {"summary": [{"model_id": "fusion", "source_stratum": "diary",
                                     "model_version": "v1", "joint_mae_min": value,
                                     "n_nights": 2}], "raw": {}, "mode": "prospective_demo"}

            with patch("researchbench.participant_prospective.run_prospective", side_effect=fake_run):
                result = run_multi_participant_synthetic(source, {"seed": 9})
            self.assertEqual(len(seen), 2)
            self.assertEqual(result["participants_total"], 2)
            self.assertEqual(result["rows_missing_participant_id"], 1)
            self.assertEqual(result["summary"][0]["joint_mae_min"], 20.0)
            self.assertEqual(result["summary"][0]["n_nights"], 4)
            self.assertEqual(result["source_kind"],
                             "declared_synthetic_multi_participant_playback")


if __name__ == "__main__":
    unittest.main()
