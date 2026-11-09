from __future__ import annotations

import importlib.util
import json
import math
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compare_personalization_bayes_long_history",
    ROOT / "scripts" / "compare_personalization_bayes_long_history.py",
)
bayes = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = bayes
SPEC.loader.exec_module(bayes)


def row(day: int, split: str = "train", midpoint_min: float = 60.0):
    forecast = datetime(2026, 1, 1) + timedelta(days=day)
    cutoff = forecast.replace(hour=20)
    start = cutoff + timedelta(hours=2)
    end = start + timedelta(hours=8)
    return {
        "split": split, "forecast": forecast, "cutoff": cutoff,
        "start": start, "end": end, "wake_date": end.date(),
        "target_midpoint": midpoint_min % 1440,
    }


class BayesLongHistoryTests(unittest.TestCase):
    def test_constant_posterior_matches_conjugate_formula(self):
        prior = 100.0
        history = [row(1, midpoint_min=110), row(2, midpoint_min=120)]
        calibration = {"constant_state_variance_min2": 100.0,
                       "observation_variance_min2": 25.0}
        value, diagnostics = bayes.eb_constant(history, prior, calibration)
        expected_variance = 1 / (1 / 100 + 2 / 25)
        self.assertAlmostEqual(diagnostics["posterior_variance"], expected_variance)
        self.assertAlmostEqual(value, expected_variance * 30 / 25)

    def test_kalman_uses_only_supplied_history_and_gap_increases_variance(self):
        prior = 100.0
        history = [row(1, midpoint_min=120)]
        calibration = {"constant_state_variance_min2": 100.0,
                       "observation_variance_min2": 25.0}
        initial = history[0]["end"] - timedelta(days=1)
        near_value, near = bayes.kalman_offset(
            history, history[0]["end"] + timedelta(days=1), prior, calibration, .01, initial)
        far_value, far = bayes.kalman_offset(
            history, history[0]["end"] + timedelta(days=30), prior, calibration, .01, initial)
        self.assertEqual(near_value, far_value)
        self.assertGreater(far["posterior_variance"], near["posterior_variance"])

    def test_affine_without_observations_returns_identity_mapping(self):
        calibration = {"affine_intercept_prior_variance_min2": 100.0,
                       "affine_slope_prior_variance": .5,
                       "affine_observation_variance_min2": 25.0}
        prediction, diagnostics = bayes.affine_posterior([], 40.0, calibration)
        self.assertAlmostEqual(prediction, 40.0)
        self.assertAlmostEqual(diagnostics["beta0"], 0.0)
        self.assertAlmostEqual(diagnostics["beta1"], 1.0)

    def test_first_seven_are_excluded_from_bayesian_updates(self):
        rows = [row(day, midpoint_min=60 + day) for day in range(9)]
        excluded = bayes.prior_dates(rows)
        target = row(12, "validation", midpoint_min=80)
        history = bayes.post_prior_history(rows, target["cutoff"], excluded)
        self.assertEqual(len(excluded), 7)
        self.assertEqual([item["forecast"] for item in history], [rows[7]["forecast"], rows[8]["forecast"]])

    def test_train_seam_guard_rejects_nonlocal_offsets(self):
        rows = [row(day, midpoint_min=0) for day in range(7)]
        rows += [row(day, midpoint_min=700) for day in range(7, 12)]
        excluded = bayes.prior_dates(rows)
        with self.assertRaisesRegex(ValueError, "circular seam"):
            bayes.calibrate_train(rows, 0.0, excluded)

    def test_train_range_guard_rejects_two_sided_wide_support(self):
        rows = [row(day, midpoint_min=0) for day in range(7)]
        rows += [row(7, midpoint_min=400), row(8, midpoint_min=1040),
                 row(9, midpoint_min=400)]
        excluded = bayes.prior_dates(rows)
        with self.assertRaisesRegex(ValueError, "range=800"):
            bayes.calibrate_train(rows, 0.0, excluded)

    def test_candidate_selection_rejects_test_rows(self):
        records = []
        for setting in (bayes.CURRENT, *bayes.CANDIDATES):
            records.append({
                "setting": setting, "split": "validation",
                "error_min": 10 if setting == bayes.CURRENT else 9,
                "update_count": 1, "decayed_mass": 1.0,
                "posterior_variance": 1.0, "gain": .1,
                "beta0": 0.0, "beta1": 1.0,
            })
        selected, _ = bayes.choose_candidate(records)
        self.assertEqual(selected, bayes.CANDIDATES[0])
        records[0]["split"] = "test"
        with self.assertRaisesRegex(ValueError, "validation only"):
            bayes.choose_candidate(records)

    def test_protocol_candidate_contract_is_enforced(self):
        protocol = json.loads(bayes.DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
        bayes._protocol_matches(protocol)
        guard = protocol["train_only_hyperparameters"]["circular_gaussian_guard"]
        self.assertIn("600", guard)
        self.assertIn("720", guard)
        protocol["alternatives"][0]["id"] = "tampered"
        with self.assertRaisesRegex(ValueError, "candidates"):
            bayes._protocol_matches(protocol)

    def test_real_train_calibration_is_finite_and_test_not_used(self):
        rows = bayes.LONG.load_rows(bayes.DEFAULT_INPUT)
        excluded = bayes.prior_dates(rows)
        prior = bayes.LONG.fixed_prior(rows, "first7_train")
        calibration = bayes.calibrate_train(rows, prior, excluded)
        self.assertEqual(calibration["train_post_prior_n"], 130)
        self.assertLess(calibration["train_max_abs_offset_min"], bayes.SEAM_GUARD_MIN)
        self.assertLess(calibration["train_offset_range_min"], bayes.OFFSET_RANGE_GUARD_MIN)
        self.assertTrue(all(math.isfinite(value) for value in calibration.values()
                            if isinstance(value, (int, float))))


if __name__ == "__main__":
    unittest.main()
