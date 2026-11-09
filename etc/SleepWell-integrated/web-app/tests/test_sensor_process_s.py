import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from night_core import ZONE, features


class SensorProcessSPathTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = datetime(2026, 10, 28, 20, 0, tzinfo=ZONE)
        self.prior = {"bed_offset": 180.0, "wake_offset": 660.0}

    def observation(self, start, binary, probability, calibrated=True, available_at=None):
        end = start + timedelta(minutes=5)
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "available_at": (available_at or end).isoformat(),
            "sleep_binary": binary,
            "sleep_probability": probability,
            "probability_calibrated": calibrated,
            "observation_model_id": "mesa_psg_activity_light_hgb_300s",
            "calibration_id": "validation_isotonic_20261028" if calibrated else None,
        }

    def test_contiguous_observations_auto_select_probability_and_diary_is_selectable(self):
        observations = [
            self.observation(self.cutoff - timedelta(minutes=10), 1, 0.8),
            self.observation(self.cutoff - timedelta(minutes=5), 0, 0.2),
        ]
        baseline_values, baseline_state = features([], self.cutoff, self.prior)
        values, state = features([], self.cutoff, self.prior, sensor_observations=observations)
        diary_values, diary_state = features(
            [], self.cutoff, self.prior, sensor_observations=observations,
            process_s_mode="diary",
        )
        self.assertNotIn("process_s_paths", baseline_state)
        self.assertEqual(diary_values, baseline_values)
        self.assertEqual(diary_state["process_s_paths"]["applied_path"], "S_diary")
        result = state["process_s_paths"]
        self.assertEqual(result["official_path"], "S_sensor_probability")
        self.assertEqual(result["official_s"], values["current_s"])
        self.assertEqual(result["requested_mode"], "auto")
        self.assertEqual(result["recommended_mode"], "auto")
        self.assertEqual(result["applied_path"], "S_sensor_probability")
        self.assertFalse(result["selection_fallback_used"])
        self.assertFalse(result["fallback_used"])
        self.assertEqual(result["fallback_reasons"], [])
        self.assertEqual(result["audit"]["observations_used"], 2)
        self.assertEqual(result["audit"]["gaps"], [])
        self.assertFalse(result["audit"]["official_forecast_unchanged"])
        self.assertIsNotNone(result["paths"]["S_diary"]["s"])
        self.assertIsNotNone(result["paths"]["S_sensor_binary"]["s"])
        self.assertIsNotNone(result["paths"]["S_sensor_probability"]["s"])

    def test_binary_path_can_be_selected_explicitly(self):
        observations = [self.observation(self.cutoff - timedelta(minutes=5), 1, 0.9)]
        values, state = features(
            [], self.cutoff, self.prior, sensor_observations=observations,
            process_s_mode="sensor_binary",
        )
        result = state["process_s_paths"]
        self.assertEqual(result["requested_path"], "S_sensor_binary")
        self.assertEqual(result["applied_path"], "S_sensor_binary")
        self.assertEqual(values["current_s"], result["paths"]["S_sensor_binary"]["s"])

    def test_gap_creates_bounds_and_falls_back_to_diary_without_imputation(self):
        observations = [
            self.observation(self.cutoff - timedelta(minutes=20), 1, 0.9),
            self.observation(self.cutoff - timedelta(minutes=5), 0, 0.1),
        ]
        values, state = features(
            [], self.cutoff, self.prior, sensor_observations=observations,
            sensor_uncertainty_threshold=1.0,
        )
        result = state["process_s_paths"]
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["research_selected_path"], "S_diary")
        self.assertIn("noncontinuous_sensor_support", result["fallback_reasons"])
        self.assertEqual(result["official_s"], values["current_s"])
        self.assertEqual(len(result["audit"]["gaps"]), 1)
        self.assertEqual(result["audit"]["gaps"][0]["minutes"], 10.0)
        for name in ("S_sensor_binary", "S_sensor_probability"):
            path = result["paths"][name]
            self.assertIsNone(path["s"])
            self.assertEqual(path["status"], "bounded_only")
            self.assertLess(path["bounds"][0], path["bounds"][1])

    def test_large_gap_records_uncertainty_threshold_fallback_reason(self):
        observations = [
            self.observation(self.cutoff - timedelta(hours=6), 1, 0.9),
            self.observation(self.cutoff - timedelta(minutes=5), 0, 0.1),
        ]
        _, state = features(
            [], self.cutoff, self.prior, sensor_observations=observations,
            sensor_uncertainty_threshold=0.05,
        )
        result = state["process_s_paths"]
        self.assertTrue(result["fallback_used"])
        self.assertIn("sensor_uncertainty_exceeds_threshold", result["fallback_reasons"])
        self.assertTrue(result["paths"]["S_sensor_probability"]["uncertainty_exceeds_threshold"])

    def test_uncalibrated_probability_is_never_selected(self):
        observations = [
            self.observation(self.cutoff - timedelta(minutes=5), 1, 0.9, calibrated=False),
        ]
        _, state = features([], self.cutoff, self.prior, sensor_observations=observations)
        result = state["process_s_paths"]
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["paths"]["S_sensor_probability"]["status"], "uncalibrated_probability")
        self.assertIn("probability_not_validation_calibrated", result["fallback_reasons"])
        self.assertIsNotNone(result["paths"]["S_sensor_binary"]["s"])

    def test_observation_not_available_by_cutoff_is_excluded_and_recorded(self):
        start = self.cutoff - timedelta(minutes=5)
        observations = [self.observation(
            start, 0, 0.1, available_at=self.cutoff + timedelta(seconds=1))]
        _, state = features([], self.cutoff, self.prior, sensor_observations=observations)
        result = state["process_s_paths"]
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_reasons"], ["no_causal_observations"])
        self.assertEqual(result["audit"]["observations_used"], 0)
        self.assertEqual(result["audit"]["observations_excluded"][0]["reason"], "not_available_by_cutoff")

    def test_tail_without_observation_is_a_gap_not_assumed_wake(self):
        observations = [self.observation(self.cutoff - timedelta(minutes=10), 0, 0.1)]
        _, state = features([], self.cutoff, self.prior, sensor_observations=observations)
        result = state["process_s_paths"]
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["audit"]["gaps"][0]["minutes"], 5.0)
        self.assertIsNone(result["paths"]["S_sensor_probability"]["s"])

    def test_nonbinary_numeric_value_is_excluded_not_truncated(self):
        row = self.observation(self.cutoff - timedelta(minutes=5), 0, 0.1)
        row["sleep_binary"] = 0.5
        _, state = features([], self.cutoff, self.prior, sensor_observations=[row])
        result = state["process_s_paths"]
        self.assertEqual(result["fallback_reasons"], ["no_causal_observations"])
        self.assertEqual(result["audit"]["observations_excluded"][0]["reason"],
                         "binary_not_zero_or_one")

    def test_unknown_selection_mode_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Unsupported Process S mode"):
            features([], self.cutoff, self.prior, process_s_mode="best_guess")

    def test_overlapping_windows_fail_closed(self):
        observations = [
            self.observation(self.cutoff - timedelta(minutes=10), 0, 0.1),
            self.observation(self.cutoff - timedelta(minutes=8), 1, 0.9),
        ]
        with self.assertRaisesRegex(ValueError, "overlap"):
            features([], self.cutoff, self.prior, sensor_observations=observations)


if __name__ == "__main__":
    unittest.main()
