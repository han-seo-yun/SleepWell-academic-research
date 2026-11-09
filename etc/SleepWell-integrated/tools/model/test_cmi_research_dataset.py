import unittest
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from cmi_research_dataset import (
    aggregate_complete_bins,
    complete_nights,
    Night,
    process_subject,
    known_intervals,
    stable_subject_split,
)


class CmiResearchDatasetTests(unittest.TestCase):
    def test_same_night_complete_pair_and_endpoint_contract(self):
        events = pd.DataFrame({
            "series_id": ["a", "a", "a", "a"], "night": [1, 1, 2, 2],
            "event": ["onset", "wakeup", "onset", "wakeup"], "step": [6, 12, 18, 24],
        })
        nights = complete_nights(events)["a"]
        starts, ends, labels = known_intervals(nights)
        self.assertEqual(list(zip(starts, ends, labels)), [(6, 12, 1), (12, 18, 0), (18, 24, 1)])

    def test_missing_night_does_not_create_wake_interval(self):
        events = pd.DataFrame({
            "series_id": ["a", "a", "a", "a"], "night": [1, 1, 3, 3],
            "event": ["onset", "wakeup", "onset", "wakeup"], "step": [6, 12, 24, 30],
        })
        starts, ends, labels = known_intervals(complete_nights(events)["a"])
        self.assertEqual(list(zip(starts, ends, labels)), [(6, 12, 1), (24, 30, 1)])

    def test_incomplete_night_is_excluded(self):
        events = pd.DataFrame({
            "series_id": ["a", "a", "a"], "night": [1, 1, 2],
            "event": ["onset", "wakeup", "onset"], "step": [6, 12, np.nan],
        })
        nights = complete_nights(events)["a"]
        self.assertEqual([(night.night, night.onset, night.wakeup) for night in nights], [(1, 6, 12)])

    def test_duplicate_cannot_become_complete_after_missing_row_drop(self):
        events = pd.DataFrame({
            "series_id": ["a", "a", "a"], "night": [1, 1, 1],
            "event": ["onset", "onset", "wakeup"], "step": [6, np.nan, 12],
        })
        self.assertNotIn("a", complete_nights(events))

    def test_complete_bin_requires_six_consecutive_known_finite_samples(self):
        steps = np.arange(12, dtype=np.int64)
        labels = np.array([0] * 6 + [1] * 6, dtype=np.int8)
        dataset = aggregate_complete_bins("a", "train", steps, np.arange(12.0), np.arange(12.0), labels)
        self.assertEqual(dataset["step_start"].tolist(), [0, 6])
        self.assertEqual(dataset["is_sleep"].tolist(), [0, 1])
        self.assertEqual(dataset["source_5s_rows"].tolist(), [6, 6])
        labels[2] = -1
        self.assertEqual(len(aggregate_complete_bins("a", "train", steps, np.arange(12.0), np.arange(12.0), labels)), 1)

    def test_tie_is_dropped_and_split_is_stable(self):
        tie = aggregate_complete_bins(
            "a", "train", np.arange(6, dtype=np.int64), np.ones(6), np.ones(6), np.array([0, 0, 0, 1, 1, 1], dtype=np.int8)
        )
        self.assertEqual(len(tie), 0)
        subjects = [f"s{i}" for i in range(20)]
        self.assertEqual(stable_subject_split(subjects), stable_subject_split(reversed(subjects)))
        first = stable_subject_split(subjects)
        self.assertEqual(len(first), 20)
        self.assertTrue(set(first.values()).issubset({"train", "validation", "test"}))
        extended = stable_subject_split([*subjects, "new_subject"])
        self.assertEqual(first, {subject: extended[subject] for subject in subjects})

    def test_timestamp_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.parquet"
            pd.DataFrame({
                "series_id": ["a"] * 6, "step": np.arange(6, dtype=np.uint32),
                "timestamp": [f"2020-01-01T00:00:{second:02d}+0000" for second in range(0, 30, 5)],
                "anglez": np.ones(6), "enmo": np.ones(6),
            }).to_parquet(path, index=False)
            with self.assertRaisesRegex(ValueError, "event timestamp"):
                process_subject(path, [Night(1, 0, 6)], "train", {0: pd.Timestamp("2020-01-02T00:00:00Z")})


if __name__ == "__main__":
    unittest.main()
