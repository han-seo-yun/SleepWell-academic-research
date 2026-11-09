import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from night_features_v2 import extended_features, weighted_median, DATA_V2
from night_core import local_time


class NewFeaturesTests(unittest.TestCase):
    def test_weighted_median_resists_single_extreme(self):
        self.assertEqual(weighted_median([100, 105, 900], [1, 1, 1]), 105)

    def test_late_and_future_rows_cannot_affect_any_feature(self):
        cutoff = local_time("2026-10-20T20:00:00+09:00")
        prior = {"bed_offset": 180, "wake_offset": 660}
        enrollment = "2026-09-01T20:00:00+09:00"
        records = []
        for i in range(1, 20):
            wake = cutoff.replace(hour=7) - timedelta(days=i)
            records.append({"bed": (wake-timedelta(hours=8)).isoformat(), "wake": wake.isoformat(),
                            "observed_at": wake.isoformat(), "source": "research"})
        expected, state = extended_features(records, cutoff, prior, enrollment)
        polluted = records + [dict(records[-1], observed_at=(cutoff+timedelta(hours=1)).isoformat(),
                                   bed=(cutoff-timedelta(hours=12)).isoformat())]
        actual, _ = extended_features(polluted, cutoff, prior, enrollment)
        self.assertEqual(actual, expected)
        self.assertEqual(state["valid_days"], 19)
        self.assertTrue(all(name in actual for name in DATA_V2))


if __name__ == "__main__":
    unittest.main()
