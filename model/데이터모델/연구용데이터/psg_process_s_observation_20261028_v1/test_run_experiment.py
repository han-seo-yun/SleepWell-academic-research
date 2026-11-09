import unittest
from datetime import datetime

import numpy as np

import run_experiment as experiment


class ProcessSObservationTests(unittest.TestCase):
    def test_probability_endpoints_equal_binary_updates(self):
        for state in (0.2, 0.5, 0.8):
            self.assertAlmostEqual(
                experiment.update_s(state, 1.0),
                state * np.exp(-experiment.STEP_H / experiment.TAU_SLEEP_H),
            )
            self.assertAlmostEqual(
                experiment.update_s(state, 0.0),
                1 - (1 - state) * np.exp(-experiment.STEP_H / experiment.TAU_WAKE_H),
            )
            middle = experiment.update_s(state, 0.4)
            self.assertGreaterEqual(middle, min(experiment.update_s(state, 0), experiment.update_s(state, 1)))
            self.assertLessEqual(middle, max(experiment.update_s(state, 0), experiment.update_s(state, 1)))

    def test_contiguous_segments_never_bridge_gaps(self):
        lines = np.array([1, 2, 3, 11, 12, 30])
        segments = experiment.contiguous_segments(lines)
        self.assertEqual([part.tolist() for part in segments], [[0, 1, 2], [3, 4], [5]])

    def test_next_twenty_is_strict(self):
        self.assertEqual(
            experiment.next_twenty_strictly_after(datetime(2000, 1, 2, 7)),
            datetime(2000, 1, 2, 20),
        )
        self.assertEqual(
            experiment.next_twenty_strictly_after(datetime(2000, 1, 2, 20)),
            datetime(2000, 1, 3, 20),
        )

    def test_five_minute_aggregation_and_projection(self):
        arrays = {
            "line": np.arange(1, 21),
            "x": np.tile(np.arange(5, dtype=float), (20, 1)),
            "y_sleep": np.r_[np.zeros(4), np.ones(6), np.zeros(10)],
        }
        epoch = experiment.aggregate_five_minute(arrays)
        self.assertEqual(epoch["line_start"].tolist(), [1, 11])
        self.assertEqual(epoch["majority"].tolist(), [1, 0])
        self.assertEqual(epoch["sleep_fraction"].tolist(), [0.6, 0.0])
        projected = experiment.project_bins(arrays["line"], epoch["line_start"], np.array([0.7, 0.1]))
        np.testing.assert_allclose(projected[:10], 0.7)
        np.testing.assert_allclose(projected[10:], 0.1)

    def test_missing_five_minute_prediction_fails_closed(self):
        with self.assertRaises(ValueError):
            experiment.project_bins(np.array([1, 2, 11]), np.array([1]), np.array([0.5]))

    def test_trajectory_is_bounded(self):
        for initial in (0.2, 0.5, 0.8):
            result = experiment.trajectory(np.linspace(0, 1, 100), initial)
            self.assertTrue(np.isfinite(result).all())
            self.assertTrue(((0 <= result) & (result <= 1)).all())


if __name__ == "__main__":
    unittest.main()
