from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compare_personalization_long_history",
    ROOT / "scripts" / "compare_personalization_long_history.py",
)
long_history = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = long_history
SPEC.loader.exec_module(long_history)


def row(day: int, split: str = "train", *, midpoint_min: float = 60.0):
    forecast = datetime(2026, 1, 1) + timedelta(days=day)
    cutoff = forecast.replace(hour=20)
    start = cutoff + timedelta(hours=2)
    end = start + timedelta(hours=8)
    midpoint_time = forecast + timedelta(minutes=midpoint_min)
    # Only target_midpoint matters to unit prediction; interval stays causal.
    return {
        "split": split, "forecast": forecast, "cutoff": cutoff,
        "start": start, "end": end, "wake_date": end.date(),
        "target_midpoint": long_history.clock_minute(midpoint_time),
    }


class LongHistoryComparisonTests(unittest.TestCase):
    def test_timezone_aware_values_convert_to_seoul_before_dropping_timezone(self):
        utc = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(long_history._naive(utc, "timestamp"), datetime(2026, 1, 2, 0, 0))

    def test_circular_midpoint_and_delta_wrap_midnight(self):
        start = datetime(2026, 1, 1, 23, 0)
        end = datetime(2026, 1, 2, 7, 0)
        self.assertAlmostEqual(long_history.midpoint(start, end), 180.0)
        self.assertEqual(long_history.circular_delta(10, 1430), 20)

    def test_future_and_current_outcomes_are_excluded(self):
        rows = [row(0), row(1), row(2, "validation")]
        history = long_history.eligible_history(rows, rows[2]["cutoff"])
        self.assertEqual(len(history), 2)
        self.assertTrue(all(item["end"] < rows[2]["cutoff"] for item in history))

    def test_first_seven_prior_uses_train_only(self):
        rows = [row(i, "train", midpoint_min=60 + i) for i in range(8)]
        rows += [row(20, "validation", midpoint_min=700), row(21, "test", midpoint_min=800)]
        expected = long_history.circular_mean([60 + i for i in range(7)])
        self.assertAlmostEqual(long_history.fixed_prior(rows, "first7_train"), expected)

    def test_gap_reduces_mass_but_count_weight_does_not_change(self):
        count = 56
        mass = sum(2 ** (-age / 14) for age in range(count))
        current0 = long_history.alpha(long_history.CURRENT, mass, count)[0]
        current56 = long_history.alpha(long_history.CURRENT, mass / 16, count)[0]
        count0 = long_history.alpha("count_weight", mass, count)[0]
        count56 = long_history.alpha("count_weight", mass / 16, count)[0]
        self.assertLess(current56, current0)
        self.assertEqual(count56, count0)

    def test_all_growth_alternatives_approach_high_weight_with_long_dense_history(self):
        count = 365
        mass = sum(2 ** (-age / 14) for age in range(count))
        self.assertLess(long_history.alpha(long_history.CURRENT, mass, count)[0], 0.60)
        for setting in long_history.CANDIDATES:
            self.assertGreater(long_history.alpha(setting, mass, count)[0], 0.95)

    def test_prediction_uses_only_completed_prior_rows(self):
        rows = [row(0, midpoint_min=100), row(1, midpoint_min=200),
                row(2, "validation", midpoint_min=300)]
        result = long_history.predict_row(rows, rows[-1], 50, long_history.CURRENT)
        self.assertEqual(result["history_count"], 2)
        self.assertGreater(result["decayed_mass"], 0)
        self.assertTrue(math.isfinite(result["error_min"]))

    def test_selection_reads_validation_records_only(self):
        records = []
        for setting in (long_history.CURRENT, *long_history.CANDIDATES):
            error = 10.0 if setting == long_history.CURRENT else 9.0
            if setting == "count_weight":
                error = 1.0
            records.append({"setting": setting, "error_min": error, "personal_weight": .5,
                            "decayed_mass": 1.0, "history_count": 1,
                            "split": "validation"})
        selected, _, _ = long_history.choose_candidate(records)
        self.assertEqual(selected["setting"], "count_weight")
        records[0]["split"] = "test"
        with self.assertRaisesRegex(ValueError, "validation records only"):
            long_history.choose_candidate(records)

    def test_frozen_input_contract_and_history_depth(self):
        rows = long_history.load_rows(long_history.DEFAULT_INPUT)
        self.assertEqual(len(rows), 196)
        self.assertEqual([sum(row["split"] == split for row in rows)
                          for split in long_history.SPLITS], [137, 29, 30])
        test = [row for row in rows if row["split"] == "test"]
        self.assertTrue(all(len(long_history.eligible_history(rows, row["cutoff"])) >= 90
                            for row in test))

    def test_protocol_declared_candidates_match_runtime(self):
        import json
        protocol = json.loads(long_history.DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
        long_history._protocol_matches(protocol)
        protocol["alternatives"][0]["id"] = "tampered"
        with self.assertRaisesRegex(ValueError, "candidate"):
            long_history._protocol_matches(protocol)


if __name__ == "__main__":
    unittest.main()
