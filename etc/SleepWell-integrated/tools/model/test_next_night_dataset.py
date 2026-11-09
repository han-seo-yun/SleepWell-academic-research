import tempfile
import unittest
from pathlib import Path

import pandas as pd

from next_night_dataset import (
    build_cmi_episodes,
    build_samsung_episodes,
    extract_mesa_episodes,
    finalize_episodes,
    local_wall_timestamp,
)


class NextNightDatasetTests(unittest.TestCase):
    def test_cmi_requires_exact_pair_preserves_split_and_wall_clock(self):
        events = pd.DataFrame({
            "series_id": ["a", "a", "a", "a", "b", "b"],
            "night": [1, 1, 2, 2, 1, 1],
            "event": ["onset", "wakeup", "onset", "onset", "onset", "wakeup"],
            "timestamp": [
                "2026-01-01T23:00:00-0400", "2026-01-02T07:00:00-0400",
                "2026-01-02T23:00:00-0400", "2026-01-02T23:05:00-0400",
                "2026-01-01T22:00:00+0200", "2026-01-02T06:00:00+0200",
            ],
        })
        result, qc = build_cmi_episodes(events, {"a": "test"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result.loc[0, "split"], "test")
        self.assertEqual(result.loc[0, "bed"].hour, 23)
        self.assertEqual(result.loc[0, "bed"].utcoffset().total_seconds(), 9 * 3600)
        self.assertEqual(qc["invalid_pair_groups"], 1)

    def test_longest_episode_wins_and_noon_uses_previous_cutoff(self):
        fixed = lambda value: local_wall_timestamp(value + "+0900")
        frame = pd.DataFrame([
            {"cohort": "x", "subject_id": "s", "split": "train", "bed": fixed("2026-01-02T10:00:00"), "wake": fixed("2026-01-02T12:00:00"), "label_source": "x"},
            {"cohort": "x", "subject_id": "s", "split": "train", "bed": fixed("2026-01-02T11:00:00"), "wake": fixed("2026-01-02T12:00:00"), "label_source": "x"},
        ])
        result, qc = finalize_episodes(frame)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.loc[0, "cutoff"].day, 1)
        self.assertEqual(result.loc[0, "bedtime_min"], 840)
        self.assertEqual(qc["duplicate_night_rows"], 1)

    def test_samsung_preserves_reference_cutoffs_and_splits(self):
        raw = pd.DataFrame({
            "sleep_start": pd.to_datetime(["2026-01-01 23:00", "2026-01-02 23:30", "2026-01-03 12:00"]),
            "sleep_end": pd.to_datetime(["2026-01-02 07:00", "2026-01-03 07:30", "2026-01-03 13:00"]),
            "episode_interval_min": [480.0, 480.0, 60.0],
            "is_main_sleep": [True, True, False], "episode_qc_pass": [True, True, True],
        })
        reference = pd.DataFrame({
            "cutoff": pd.to_datetime(["2026-01-01 20:00", "2026-01-02 20:00"]),
            "split": ["train", "test"],
        })
        result, _ = build_samsung_episodes(raw, reference)
        self.assertEqual(result["split"].tolist(), ["train", "test"])

    def test_mesa_rejects_gap_and_offwrist_and_uses_last_sleep_plus_epoch(self):
        rows = [
            [1, 1, "22:00:00", "2000-01-01", 0, 1, "REST"],
            [1, 2, "22:00:30", "2000-01-01", 0, 0, "REST-S"],
            [1, 3, "22:01:00", "2000-01-01", 0, 0, "REST-S"],
            [1, 4, "22:01:30", "2000-01-01", 1, 0, "REST-S"],
        ]
        start = pd.Timestamp("2000-01-01 23:00:00")
        for offset in range(61):
            stamp = start + pd.Timedelta(seconds=30 * offset)
            rows.append([1, 6 + offset, stamp.strftime("%H:%M:%S"), "2000-01-01", 0, 0, "REST-S"])
        rows.append([1, 67, "23:30:30", "2000-01-01", 0, 1, "ACTIVE"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mesa-sleep-0001.csv"
            pd.DataFrame(rows, columns=["mesaid", "line", "linetime", "date_shifted", "offwrist", "wake", "interval"]).to_csv(path, index=False)
            result, qc, _ = extract_mesa_episodes(path, "1", "validation", 70.0, "female")
        self.assertEqual(len(result), 1)
        self.assertEqual(result.loc[0, "split"], "validation")
        self.assertEqual(result.loc[0, "duration_min"], 30.5)
        self.assertGreaterEqual(qc["cadence_breaks"], 1)

    def test_local_wall_clock_does_not_convert_source_offset(self):
        stamp = local_wall_timestamp("2026-01-01T23:15:00-0400")
        self.assertEqual((stamp.hour, stamp.minute), (23, 15))
        self.assertEqual(stamp.utcoffset().total_seconds(), 9 * 3600)


if __name__ == "__main__":
    unittest.main()
