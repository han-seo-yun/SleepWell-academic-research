"""The recent comparator uses observations, not seven consecutive calendar days."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_lab import evaluate_run


def record(day, onset_minutes):
    cutoff = datetime.fromisoformat(day + "T20:00:00+09:00")
    onset = cutoff + timedelta(minutes=onset_minutes)
    wake = onset + timedelta(hours=8)
    return {"participant_id": "P01", "cutoff": cutoff.isoformat(),
            "sleep_start": onset.isoformat(), "sleep_end": wake.isoformat(),
            "observed_at": (wake + timedelta(hours=1)).isoformat()}


class BaselineWindowTests(unittest.TestCase):
    def test_sparse_observations_are_not_a_seven_calendar_day_window(self):
        result = evaluate_run({"schema_version": 1, "mode": "real", "cohorts": ["cmi"],
                               "records": [record("2026-01-01", 120), record("2026-01-08", 240),
                                           record("2026-01-15", 180), record("2026-02-15", 180)]})
        baseline = [row for row in result["predictions"] if row["model_id"] == "baseline.recent7"]
        self.assertIsNone(baseline[0]["onset_offset_min"])
        self.assertAlmostEqual(baseline[2]["onset_offset_min"], 180)
        self.assertAlmostEqual(baseline[2]["wake_offset_min"], 660)
        self.assertIsNone(baseline[3]["onset_offset_min"])
        self.assertEqual(result["summary"]["common_nights"], 2)
        self.assertTrue(all(metric["n"] == 2 for metric in result["metrics"]))
        label = next(model["label"] for model in result["models"] if model["id"] == "baseline.recent7")
        self.assertIn("28일", label)
        self.assertIn("7회", label)


if __name__ == "__main__":
    unittest.main()
