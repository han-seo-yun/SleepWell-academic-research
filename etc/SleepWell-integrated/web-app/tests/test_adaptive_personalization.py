import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adaptive_personalization import POLICY_VERSION, compute_adjustment


KST = timezone(timedelta(hours=9))


def history(count=14):
    cutoff = datetime(2026, 4, 1, 20, tzinfo=KST)
    rows = []
    for day in range(count, 0, -1):
        rows.append({
            "night_id": str(day),
            "available_at": (cutoff - timedelta(days=day, hours=8)).isoformat(),
            "onset_residual_min": 20.0,
            "wake_residual_min": -10.0,
        })
    return cutoff, rows


class AdaptivePersonalizationTests(unittest.TestCase):
    def test_default_activates_after_first_resolved_night_and_accumulates(self):
        cutoff, rows = history()
        empty = compute_adjustment([], cutoff)
        first = compute_adjustment(rows[:1], cutoff)
        active = compute_adjustment(rows, cutoff)
        self.assertFalse(empty["active"])
        self.assertEqual(empty["onset_correction_min"], 0.0)
        self.assertTrue(first["active"])
        self.assertGreater(first["onset_correction_min"], 0.0)
        self.assertLess(first["weight"], active["weight"])
        self.assertTrue(active["active"])
        self.assertGreater(active["onset_correction_min"], 0.0)
        self.assertLess(active["onset_correction_min"], 20.0)
        self.assertNotEqual(active["decayed_mass"], active["kish_effective_n"])
        self.assertEqual(active["policy_version"], POLICY_VERSION)
        self.assertEqual(active["minimum_history"], 1)

    def test_residuals_and_final_correction_are_bounded(self):
        cutoff, rows = history(8)
        for row in rows:
            row["onset_residual_min"] = 10000.0
            row["wake_residual_min"] = -10000.0
        result = compute_adjustment(rows, cutoff)
        self.assertEqual(result["raw_onset_residual_mean_min"], 180.0)
        self.assertEqual(result["raw_wake_residual_mean_min"], -180.0)
        self.assertLessEqual(result["onset_correction_min"], 60.0)
        self.assertGreaterEqual(result["wake_correction_min"], -60.0)
        self.assertEqual(result["residual_clip_min"], 180.0)
        self.assertEqual(result["correction_cap_min"], 60.0)

    def test_current_and_future_outcomes_cannot_change_adjustment(self):
        cutoff, rows = history()
        expected = compute_adjustment(rows, cutoff)
        changed = copy.deepcopy(rows)
        changed.extend([
            {"night_id": "current", "available_at": cutoff.isoformat(),
             "onset_residual_min": 9999.0, "wake_residual_min": 9999.0},
            {"night_id": "future", "available_at": (cutoff + timedelta(days=1)).isoformat(),
             "onset_residual_min": -9999.0, "wake_residual_min": -9999.0},
        ])
        self.assertEqual(expected, compute_adjustment(changed, cutoff))

    def test_order_is_irrelevant_and_latest_revision_per_night_wins(self):
        cutoff, rows = history()
        rows.append({**rows[0], "available_at": (cutoff - timedelta(days=13)).isoformat(),
                     "onset_residual_min": 30.0})
        first = compute_adjustment(rows, cutoff)
        second = compute_adjustment(list(reversed(rows)), cutoff)
        self.assertEqual(first, second)
        self.assertEqual(first["eligible_count"], 14)

    def test_uncertainty_is_unknown_instead_of_fabricated(self):
        cutoff, rows = history()
        result = compute_adjustment(rows, cutoff)
        self.assertIsNone(result["uncertainty_min"])
        self.assertIn("없어", result["uncertainty_reason"])

    def test_late_report_is_weighted_by_outcome_time(self):
        cutoff, rows = history()
        old_outcome = cutoff - timedelta(days=30)
        rows[-1]["outcome_at"] = old_outcome.isoformat()
        rows[-1]["available_at"] = (cutoff - timedelta(hours=1)).isoformat()
        rows[-1]["onset_residual_min"] = 1000.0
        result = compute_adjustment(rows, cutoff)
        self.assertGreater(result["last_available_gap_days"], 0)
        self.assertLess(result["last_available_gap_days"], 1)
        self.assertGreaterEqual(result["last_outcome_gap_days"], 1)
        self.assertLess(result["raw_onset_residual_mean_min"], 100.0)

    def test_outcome_after_availability_is_invalid(self):
        cutoff, rows = history()
        rows[-1]["outcome_at"] = (cutoff - timedelta(hours=1)).isoformat()
        rows[-1]["available_at"] = (cutoff - timedelta(hours=2)).isoformat()
        self.assertEqual(compute_adjustment(rows, cutoff)["eligible_count"], 13)

    def test_timezone_is_required(self):
        cutoff, rows = history()
        with self.assertRaises(ValueError):
            compute_adjustment(rows, cutoff.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
