import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from research_tabular import (personal_features, subject_split, chronological_split,
                              paired_ci, run_regression)


class ResearchTabularTests(unittest.TestCase):
    def tables(self):
        starts = pd.to_datetime(["2026-01-01 23:00", "2026-01-02 23:00", "2026-03-01 23:00"])
        e = pd.DataFrame({"sleep_start": starts, "sleep_end": starts+pd.Timedelta(hours=8),
                          "episode_interval_min": 480., "is_main_sleep": True,
                          "episode_qc_pass": True})
        hr = pd.DataFrame({"timestamp": pd.to_datetime(["2026-01-02 19:59", "2026-01-02 20:00"]),
                           "heart_rate": [60., 999.], "hr_qc_pass": True})
        st = pd.DataFrame({"timestamp": pd.to_datetime(["2026-01-02 19:50", "2026-01-02 19:55"]),
                           "steps_10min": [100., 999.], "steps_qc_pass": True})
        return e, hr, st

    def test_cutoff_completed_intervals_and_fresh_history(self):
        d = personal_features(*self.tables())
        self.assertEqual(d.loc[1, "steps"], 100)
        self.assertEqual(d.loc[1, "hr_mean"], 60)
        self.assertEqual(d.loc[1, "history_count"], 1)
        self.assertEqual(d.loc[2, "history_count"], 0)
        self.assertTrue(np.isnan(d.loc[0, "prior_bedtime_min"]))
        self.assertEqual(d.loc[1, "bedtime_min"], 180)
        self.assertEqual(d.loc[1, "wake_min"], 660)

    def test_same_night_targets_do_not_change_current_features(self):
        e, hr, st = self.tables()
        original = personal_features(e, hr, st)
        e.loc[1, "sleep_end"] += pd.Timedelta(hours=1)
        e.loc[1, "episode_interval_min"] += 60
        changed = personal_features(e, hr, st)
        self.assertEqual(original.loc[1, "prior_interval_min"], changed.loc[1, "prior_interval_min"])

    def test_split_stability_and_order(self):
        old = {str(i): subject_split(str(i)) for i in range(100)}
        expanded = {str(i): subject_split(str(i)) for i in range(200)}
        self.assertTrue(all(expanded[k] == v for k, v in old.items()))
        split = chronological_split(20)
        self.assertEqual(list(split[:14]), ["train"]*14)
        self.assertEqual(list(split[14:17]), ["validation"]*3)
        self.assertEqual(list(split[17:]), ["test"]*3)

    def test_paired_ci_identical_predictions(self):
        result = paired_ci([1, 2, 3], np.array([1, 1, 1]), np.array([1, 1, 1]), 2)
        self.assertEqual(result["ci95"], [0, 0])

    def test_end_to_end_selection_persisted_and_private_outputs(self):
        n = 40
        d = pd.DataFrame({"subject_id": [f"s{i}" for i in range(n)],
                          "x": np.arange(n), "y": np.arange(n)*.1,
                          "split": chronological_split(n)})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/"run"
            result = run_regression(d, {"x": ["x"]}, ["y"], root)
            self.assertEqual(result["rows"], n)
            self.assertTrue((root/"y_validation_selection.json").is_file())
            table = pd.read_csv(root/"experiment_metrics.csv")
            self.assertEqual(int(table.selected_by_validation.sum()), 1)
            with self.assertRaises(FileExistsError):
                run_regression(d, {"x": ["x"]}, ["y"], root)


if __name__ == "__main__":
    unittest.main()
