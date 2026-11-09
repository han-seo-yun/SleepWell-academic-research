import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from build_personal_pre20 import FEATURES, build_cache, write_cache


class PersonalPre20CacheTests(unittest.TestCase):
    def frames(self):
        episodes = pd.DataFrame({
            "cutoff": ["2026-01-02T11:00:00+00:00", "2026-01-03T20:00:00+09:00"],
            "bed": ["future target is unused", "future target is unused"],
        })
        heart_rate = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2026-01-01 19:59",  # prior local day
                "2026-01-02 17:00",  # late window boundary
                "2026-01-02 19:59",  # finishes exactly at cutoff
                "2026-01-02 20:00",  # future relative to cutoff
                "2026-01-02 19:58",  # QC failure
            ]),
            "heart_rate": [1.0, 60.0, 80.0, 999.0, 777.0],
            "hr_qc_pass": [True, True, True, True, False],
        })
        steps = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2026-01-01 19:50",  # prior local day
                "2026-01-02 19:40",
                "2026-01-02 19:50",  # finishes exactly at cutoff
                "2026-01-02 19:51",  # straddles cutoff
                "2026-01-02 19:30",  # QC failure
            ]),
            "steps_10min": [1.0, 0.0, 100.0, 999.0, 777.0],
            "steps_qc_pass": [True, True, True, True, False],
        })
        return episodes, heart_rate, steps

    def test_complete_bins_only_and_future_targets_ignored(self):
        episodes, heart_rate, steps = self.frames()
        result = build_cache(episodes, heart_rate, steps, expected_rows=2)
        first = result.iloc[0]
        self.assertEqual(first.cutoff, "2026-01-02T20:00:00+09:00")
        self.assertEqual(first.hr_mean, 70.0)
        self.assertEqual(first.hr_count, 2)
        self.assertEqual(first.late_hr_mean, 70.0)
        self.assertEqual(first.late_hr_count, 2)
        self.assertEqual(first.steps, 100.0)
        self.assertEqual(first.step_bins, 2)
        self.assertEqual(first.active_step_bins, 1)
        self.assertEqual(list(result.columns), ["cutoff", *FEATURES])
        self.assertNotIn("bed", result.columns)

    def test_qc_and_nonfinite_values_do_not_enter_aggregates(self):
        episodes, heart_rate, steps = self.frames()
        heart_rate.loc[len(heart_rate)] = [pd.Timestamp("2026-01-02 19:57"), np.inf, True]
        steps.loc[len(steps)] = [pd.Timestamp("2026-01-02 19:20"), np.nan, True]
        result = build_cache(episodes, heart_rate, steps)
        self.assertEqual(result.loc[0, "hr_count"], 2)
        self.assertEqual(result.loc[0, "step_bins"], 2)

    def test_rejects_naive_or_non_20_cutoffs_and_duplicates(self):
        episodes, heart_rate, steps = self.frames()
        with self.assertRaisesRegex(ValueError, "explicit UTC offset"):
            build_cache(pd.DataFrame({"cutoff": ["2026-01-02 20:00"]}), heart_rate, steps)
        with self.assertRaisesRegex(ValueError, "exactly 20:00"):
            build_cache(pd.DataFrame({"cutoff": ["2026-01-02T19:59:00+09:00"]}), heart_rate, steps)
        with self.assertRaisesRegex(ValueError, "unique"):
            build_cache(pd.concat([episodes.iloc[:1], episodes.iloc[:1]]), heart_rate, steps)

    def test_writer_records_hashes_coverage_and_refuses_overwrite(self):
        episodes, heart_rate, steps = self.frames()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "personal"
            source.mkdir()
            episode_path = root / "episodes.parquet"
            episodes.to_parquet(episode_path, index=False)
            heart_rate.to_parquet(source / "heart_rate_1min.parquet", index=False)
            steps.to_parquet(source / "steps_10min.parquet", index=False)
            output = root / "cache"
            metadata = write_cache(episode_path, source, output, expected_rows=2)
            self.assertEqual(metadata["rows"], 2)
            self.assertEqual(set(metadata["source_sha256"]), {
                "episodes", "heart_rate_1min", "steps_10min"})
            self.assertEqual(metadata["coverage"]["hr_mean"]["nonmissing_rows"], 1)
            saved = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
            self.assertIn("end-of-bin", saved["availability_assumption"])
            self.assertEqual(len(pd.read_parquet(output / "features.parquet")), 2)
            with self.assertRaises(FileExistsError):
                write_cache(episode_path, source, output, expected_rows=2)


if __name__ == "__main__":
    unittest.main()
