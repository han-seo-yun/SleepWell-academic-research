from __future__ import annotations

import json
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPAT = HERE.parent / "matched_20261025" / "compat"
sys.path.insert(0, str(COMPAT))
sys.path.insert(0, str(HERE))

from daily_features import POLICY_SPECS, extended_features
from night_features_v2 import extended_features as legacy_extended_features


ZONE = timezone(timedelta(hours=9))
PRIOR = {"bed_offset": 180.0, "wake_offset": 660.0}
CUTOFF = datetime(2026, 6, 15, 20, tzinfo=ZONE)


def record(days_ago, bed_offset=240.0, wake_offset=720.0, observed_delay_hours=1.0):
    anchor = CUTOFF - timedelta(days=days_ago)
    bed = anchor.replace(hour=20, minute=0, second=0, microsecond=0) + timedelta(minutes=bed_offset)
    wake = anchor.replace(hour=20, minute=0, second=0, microsecond=0) + timedelta(minutes=wake_offset)
    return {"source": "research", "bed": bed.isoformat(), "wake": wake.isoformat(),
            "observed_at": (wake + timedelta(hours=observed_delay_hours)).isoformat()}


class DailyFeatureTests(unittest.TestCase):
    def call(self, records, policy="daily_mean_h14_l14", cutoff=CUTOFF):
        return extended_features(records, cutoff, PRIOR, CUTOFF - timedelta(days=100), policy)

    def test_policy_specs_are_json_safe(self):
        json.dumps(POLICY_SPECS, allow_nan=False)
        self.assertEqual(set(POLICY_SPECS), {"legacy14", "daily_mean_h14_l14",
                         "daily_median_h14_l14", "daily_pattern_h14_l14",
                         "daily_mean_h28_l28"})

    def test_zero_one_and_eight_records_have_no_fourteen_record_cliff(self):
        values0, state0 = self.call([])
        self.assertEqual(state0["personal_weight"], 0.0)
        self.assertEqual(state0["mode"], "population")
        self.assertEqual((values0["habit_bed"], values0["habit_wake"]),
                         (PRIOR["bed_offset"], PRIOR["wake_offset"]))

        values1, state1 = self.call([record(1)])
        expected_mass = 2.0 ** (-0.5 / 14.0)
        expected_weight = expected_mass / (expected_mass + 14.0)
        self.assertAlmostEqual(state1["decayed_mass"], expected_mass, places=12)
        self.assertAlmostEqual(state1["personal_weight"], expected_weight, places=12)
        self.assertGreater(state1["personal_weight"], 0.0)
        self.assertEqual(state1["mode"], "personalized_daily")
        self.assertGreater(values1["habit_bed"], PRIOR["bed_offset"])

        values8, state8 = self.call([record(i) for i in range(1, 9)])
        self.assertEqual(state8["valid_days"], 8)
        self.assertGreater(state8["personal_weight"], state1["personal_weight"])
        self.assertLess(state8["valid_days"], 14)

        values13, state13 = self.call([record(i) for i in range(1, 14)])
        values14, state14 = self.call([record(i) for i in range(1, 15)])
        self.assertEqual(state13["mode"], "personalized_daily")
        self.assertEqual(state14["mode"], "personalized_daily")
        self.assertLess(abs(values14["habit_bed"] - values13["habit_bed"]), 2.0)

    def test_gap_decay_shrinks_weight_and_schedule_toward_prior(self):
        recent_values, recent = self.call([record(1)])
        old_values, old = self.call([record(28)])
        self.assertGreater(recent["decayed_mass"], old["decayed_mass"])
        self.assertGreater(recent["personal_weight"], old["personal_weight"])
        self.assertGreater(abs(recent_values["habit_bed"] - PRIOR["bed_offset"]),
                           abs(old_values["habit_bed"] - PRIOR["bed_offset"]))
        self.assertAlmostEqual(recent["last_gap_days"], 0.5, places=7)

    def test_underflowed_history_mass_stays_population(self):
        values, state = extended_features([record(20000)], CUTOFF, PRIOR, None,
                                          "daily_pattern_h14_l14")
        self.assertEqual(state["valid_days"], 1)
        self.assertEqual(state["decayed_mass"], 0.0)
        self.assertEqual(state["personal_weight"], 0.0)
        self.assertEqual(state["mode"], "population")
        self.assertEqual(state["pattern_status"], "insufficient")
        self.assertEqual((values["habit_bed"], values["habit_wake"]),
                         (PRIOR["bed_offset"], PRIOR["wake_offset"]))

    def test_future_and_delayed_records_are_excluded(self):
        valid = record(2)
        future = record(-1)
        delayed = record(1, observed_delay_hours=24.0)
        _, baseline = self.call([valid])
        values, state = self.call([valid, future, delayed])
        baseline_values, _ = self.call([valid])
        self.assertEqual(state["valid_days"], 1)
        self.assertEqual(state["decayed_mass"], baseline["decayed_mass"])
        self.assertEqual(values, baseline_values)

    def test_legacy14_is_exact_delegation(self):
        records = [record(i) for i in range(1, 17)]
        expected = legacy_extended_features(records, CUTOFF, PRIOR, CUTOFF - timedelta(days=100))
        actual = extended_features(records, CUTOFF, PRIOR, CUTOFF - timedelta(days=100), "legacy14")
        self.assertEqual(actual, expected)

    def test_all_daily_policies_are_finite_and_projected(self):
        records = [record(i, bed_offset=900.0 if i % 2 else 20.0,
                          wake_offset=1380.0 if i % 2 else 500.0) for i in range(1, 9)]
        legacy_values, _ = legacy_extended_features(records, CUTOFF, PRIOR, CUTOFF - timedelta(days=100))
        allowed_changes = {"habit_bed", "habit_wake", "personal_weight", "current_s", "current_c",
                           "sleep_threshold", "wake_threshold", "physical_bed", "physical_wake"}
        for policy in POLICY_SPECS:
            if policy == "legacy14":
                continue
            with self.subTest(policy=policy):
                values, state = self.call(records, policy)
                self.assertEqual(set(values), set(legacy_values))
                changed = {key for key in values if values[key] != legacy_values[key]}
                self.assertLessEqual(changed, allowed_changes)
                self.assertTrue(all(math.isfinite(values[k]) for k in
                                    ("habit_bed", "habit_wake", "current_s", "current_c",
                                     "sleep_threshold", "wake_threshold", "physical_bed", "physical_wake")))
                self.assertLessEqual(0.0, values["habit_bed"])
                self.assertLessEqual(values["habit_bed"], 960.0)
                self.assertLessEqual(values["habit_bed"] + 30.0, values["habit_wake"])
                self.assertLessEqual(values["habit_wake"], min(values["habit_bed"] + 960.0, 1440.0))
                self.assertFalse(state["physiological_tau_estimated"])

    def test_pattern_requires_two_records_of_each_weektype(self):
        insufficient = [record(1), record(2), record(3)]
        base_values, _ = self.call(insufficient, "daily_median_h14_l14")
        values, state = self.call(insufficient, "daily_pattern_h14_l14")
        self.assertEqual(state["pattern_status"], "insufficient")
        self.assertEqual(state["pattern_weight"], 0.0)
        self.assertEqual(values["habit_bed"], base_values["habit_bed"])

        evidence = [record(i, bed_offset=300.0 if (CUTOFF - timedelta(days=i)).weekday() >= 5 else 180.0,
                           wake_offset=780.0 if (CUTOFF - timedelta(days=i)).weekday() >= 5 else 660.0)
                    for i in range(1, 15)]
        # Friday's decision targets a weekend night, whose center differs from
        # the weekday-dominated overall median in this fixture.
        pattern_cutoff = CUTOFF + timedelta(days=4)
        values, state = self.call(evidence, "daily_pattern_h14_l14", cutoff=pattern_cutoff)
        self.assertEqual(state["pattern_status"], "exploratory")
        self.assertGreaterEqual(state["pattern_weekday_count"], 2)
        self.assertGreaterEqual(state["pattern_weekend_count"], 2)
        self.assertGreater(state["pattern_weight"], 0.0)
        self.assertLessEqual(state["pattern_weight"], 0.25)
        self.assertNotEqual(state["pattern_bed_applied_shift_min"], 0.0)
        self.assertLessEqual(values["habit_bed"] + 30.0, values["habit_wake"])


if __name__ == "__main__":
    unittest.main()
