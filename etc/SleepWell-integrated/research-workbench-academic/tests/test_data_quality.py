from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from researchbench.academic_store import AcademicProject
from researchbench.data_quality import load_project_quality, summarize_quality


class DataQualityTests(unittest.TestCase):
    def test_canonical_sleep_rows_report_missing_invalid_duplicate_and_short_history(self):
        frame = pd.DataFrame([
            {"participant_id": "p1", "cutoff": "2026-01-01T20:00:00+09:00",
             "sleep_start": "2026-01-01T23:00:00+09:00", "sleep_end": "2026-01-02T07:00:00+09:00",
             "observed_at": "2026-01-02T08:00:00+09:00"},
            {"participant_id": "p1", "cutoff": "2026-01-02T20:00:00+09:00",
             "sleep_start": "", "sleep_end": "2026-01-03T07:00:00+09:00",
             "observed_at": "2026-01-03T08:00:00+09:00"},
            {"participant_id": "p2", "cutoff": "2026-01-03T20:00:00+09:00",
             "sleep_start": "2026-01-04T08:00:00+09:00", "sleep_end": "2026-01-04T07:00:00+09:00",
             "observed_at": "bad timestamp"},
        ])
        frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)

        summary = summarize_quality(frame)

        self.assertEqual(summary["rows"], 4)
        self.assertEqual(summary["participants"], 2)
        self.assertEqual(summary["missing_cells"], 1)
        self.assertAlmostEqual(summary["missing_cell_fraction"], 1 / 20)
        self.assertEqual(summary["exact_duplicate_rows"], 1)
        self.assertEqual(summary["invalid_timestamps"]["sleep_start"], 1)
        self.assertEqual(summary["invalid_timestamps"]["observed_at"], 1)
        self.assertEqual(summary["nonpositive_sleep_intervals"], 1)
        self.assertEqual(summary["participants_below_7_nights"], 2)
        self.assertFalse(summary["sleepwell_ready"])
        codes = {item["code"] for item in summary["findings"]}
        self.assertIn("invalid_timestamps", codes)
        self.assertIn("nonpositive_sleep_intervals", codes)
        self.assertIn("exact_duplicate_rows", codes)

    def test_unified_cohort_aliases_count_same_subject_separately(self):
        frame = pd.DataFrame([
            {"cohort": "CMI", "subject_id": "001", "cutoff": "2026-01-01T20:00:00+09:00",
             "bed": "2026-01-01T23:00:00+09:00", "wake": "2026-01-02T07:00:00+09:00",
             "observed_at": "2026-01-02T08:00:00+09:00"},
            {"cohort": "MESA", "subject_id": "001", "cutoff": "2026-01-01T20:00:00+09:00",
             "bed": "2026-01-01T22:30:00+09:00", "wake": "2026-01-02T06:30:00+09:00",
             "observed_at": "2026-01-02T07:00:00+09:00"},
            {"cohort": "CMI", "subject_id": "002", "cutoff": "2026-01-01T20:00:00+09:00",
             "bed": "2026-01-01T23:30:00+09:00", "wake": "2026-01-02T07:30:00+09:00",
             "observed_at": "2026-01-02T08:00:00+09:00"},
        ])

        summary = summarize_quality(frame)

        self.assertEqual(summary["participants"], 3)
        self.assertEqual(summary["field_mapping"]["sleep_start"], "bed")
        self.assertEqual(summary["field_mapping"]["sleep_end"], "wake")
        self.assertTrue(summary["sleepwell_ready"])

    def test_numeric_table_is_described_but_not_sleepwell_ready(self):
        summary = summarize_quality(pd.DataFrame({"x": [1, 2], "target": [3.0, None]}))

        self.assertEqual(summary["rows"], 2)
        self.assertIsNone(summary["participants"])
        self.assertFalse(summary["sleepwell_ready"])
        missing = next(item for item in summary["findings"] if item["code"] == "missing_required_columns")
        self.assertEqual(missing["severity"], "error")
        self.assertGreater(missing["count"], 0)

    def test_blank_required_identifier_blocks_sleepwell_ready(self):
        frame = pd.DataFrame([{
            "participant_id": "", "cutoff": "2026-01-01T20:00:00+09:00",
            "sleep_start": "2026-01-01T23:00:00+09:00",
            "sleep_end": "2026-01-02T07:00:00+09:00",
            "observed_at": "2026-01-02T08:00:00+09:00",
        }])

        summary = summarize_quality(frame)

        self.assertEqual(summary["missing_required_values"]["participant_id"], 1)
        self.assertFalse(summary["sleepwell_ready"])
        self.assertIn("missing_required_values", {item["code"] for item in summary["findings"]})

    def test_vendor_time_contract_violations_are_separate_errors(self):
        frame = pd.DataFrame([
            {"participant_id": "valid-1", "cutoff": "2026-01-01T19:00:00+09:00",
             "sleep_start": "2026-01-01T23:00:00+09:00", "sleep_end": "2026-01-02T07:00:00+09:00",
             "observed_at": "2026-01-02T08:00:00+09:00"},
            {"participant_id": "valid-2", "cutoff": "2026-01-01T20:00:00+09:00",
             "sleep_start": "2026-01-02T12:01:00+09:00", "sleep_end": "2026-01-02T13:00:00+09:00",
             "observed_at": "2026-01-02T13:30:00+09:00"},
            {"participant_id": "valid-3", "cutoff": "2026-01-01T20:00:00+09:00",
             "sleep_start": "2026-01-01T23:00:00+09:00", "sleep_end": "2026-01-01T23:29:00+09:00",
             "observed_at": "2026-01-01T23:40:00+09:00"},
            {"participant_id": "valid-4", "cutoff": "2026-01-01T20:00:00+09:00",
             "sleep_start": "2026-01-01T20:01:00+09:00", "sleep_end": "2026-01-02T20:01:00+09:00",
             "observed_at": "2026-01-02T20:02:00+09:00"},
        ])

        summary = summarize_quality(frame)
        codes = {item["code"]: item["count"] for item in summary["findings"]}

        self.assertEqual(codes["cutoff_not_2000_kst"], 1)
        self.assertEqual(codes["onset_outside_supported_horizon"], 1)
        self.assertEqual(codes["sleep_duration_outside_30_960_min"], 2)
        self.assertEqual(codes["wake_after_cutoff_1440_min"], 1)
        self.assertFalse(summary["sleepwell_ready"])

    def test_invalid_participant_id_and_empty_data_are_not_ready(self):
        invalid = pd.DataFrame([{
            "participant_id": "참여자 1", "cutoff": "2026-01-01T20:00:00+09:00",
            "sleep_start": "2026-01-01T23:00:00+09:00", "sleep_end": "2026-01-02T07:00:00+09:00",
            "observed_at": "2026-01-02T08:00:00+09:00",
        }])
        invalid_summary = summarize_quality(invalid)
        self.assertEqual(invalid_summary["invalid_participant_ids"], 1)
        self.assertFalse(invalid_summary["sleepwell_ready"])

        empty_summary = summarize_quality(pd.DataFrame(columns=invalid.columns))
        self.assertFalse(empty_summary["sleepwell_ready"])
        self.assertIn("empty_dataset", {item["code"] for item in empty_summary["findings"]})

    def test_project_loader_verifies_snapshot_hash_before_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            project = AcademicProject.create(Path(directory) / "quality.swa", "quality")
            source = Path(directory) / "input.csv"
            source.write_text(
                "participant_id,cutoff,sleep_start,sleep_end,observed_at\n"
                "p1,2026-01-01T20:00:00+09:00,2026-01-01T23:00:00+09:00,"
                "2026-01-02T07:00:00+09:00,2026-01-02T08:00:00+09:00\n",
                encoding="utf-8",
            )
            project.import_data(source)

            frame, summary = load_project_quality(project)
            self.assertEqual(len(frame), 1)
            self.assertTrue(summary["sleepwell_ready"])
            snapshot = project.snapshot_path(project.state["input"]["snapshot"])
            snapshot.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                load_project_quality(project)


if __name__ == "__main__":
    unittest.main()
