import json
import math
import unittest

import numpy as np
import pandas as pd

from personalization_ablation import POLICIES, policy_specs, replay_personalization


def make_frame(n=6, subject="a", split="validation"):
    cutoff = pd.date_range("2026-01-01T20:00:00Z", periods=n, freq="1D")
    wake = cutoff + pd.Timedelta(hours=10)
    return pd.DataFrame({
        "row_id": np.arange(n), "subject_id": subject, "split": split,
        "cutoff": cutoff, "wake": wake,
        "observed_at": wake + pd.Timedelta(hours=1),
        "bed_offset": 100.0 + np.arange(n), "wake_offset": 600.0 + np.arange(n),
    })


class PersonalizationTests(unittest.TestCase):
    def test_exact_mean3_hand_calculation_uses_original_residuals(self):
        frame = make_frame(4)
        predictions = np.tile([90.0, 590.0], (4, 1))
        corrected, trace = replay_personalization(frame, predictions, "mean3")
        weights = 2.0 ** (-np.array([2 + 14/24, 1 + 14/24, 14/24]) / 14)
        residuals = np.array([10.0, 11.0, 12.0])
        mass = weights.sum()
        alpha = mass / (mass + 14)
        expected = alpha * np.average(residuals, weights=weights)
        self.assertAlmostEqual(trace.loc[3, "mass"], mass)
        self.assertAlmostEqual(trace.loc[3, "onset_correction"], expected)
        np.testing.assert_allclose(corrected[3], [90 + expected, 590 + expected])

    def test_future_and_late_outcomes_do_not_affect_current_prediction(self):
        frame = make_frame(5)
        frame.loc[1, "observed_at"] = pd.Timestamp("2026-01-10T12:00:00Z")
        predictions = np.tile([90.0, 590.0], (5, 1))
        first, trace = replay_personalization(frame, predictions, "mean3")
        changed = frame.copy()
        changed.loc[1, ["bed_offset", "wake_offset"]] = [900.0, 1200.0]
        changed.loc[4, ["bed_offset", "wake_offset"]] = [800.0, 1300.0]
        second, _ = replay_personalization(changed, predictions, "mean3")
        np.testing.assert_allclose(first[:4], second[:4])
        self.assertEqual(trace.loc[4, "n_prior"], 3)

    def test_subject_and_split_histories_are_isolated(self):
        a_train = make_frame(3, "a", "train")
        a_val = make_frame(3, "a", "validation").assign(row_id=np.arange(10, 13))
        b_val = make_frame(3, "b", "validation").assign(row_id=np.arange(20, 23))
        frame = pd.concat([a_train, a_val, b_val], ignore_index=True)
        pred = np.tile([90.0, 590.0], (len(frame), 1))
        _, trace = replay_personalization(frame, pred, "mean3")
        self.assertEqual(trace.n_prior.tolist(), [0, 1, 2, 0, 1, 2, 0, 1, 2])
        self.assertFalse(trace.active.any())

    def test_unsorted_input_returns_original_order(self):
        frame = make_frame(5)
        pred = np.tile([90.0, 590.0], (5, 1))
        expected, expected_trace = replay_personalization(frame, pred, "mean3")
        order = [3, 0, 4, 1, 2]
        shuffled = frame.iloc[order].reset_index(drop=True)
        got, got_trace = replay_personalization(shuffled, pred[order], "mean3")
        inverse = np.argsort(order)
        np.testing.assert_allclose(got[inverse], expected)
        self.assertEqual(got_trace.iloc[inverse].row_id.tolist(), expected_trace.row_id.tolist())

    def test_gap_weighting_and_projection(self):
        frame = make_frame(4)
        frame.loc[1:, "cutoff"] += pd.Timedelta(days=20)
        frame.loc[1:, "wake"] += pd.Timedelta(days=20)
        frame.loc[1:, "observed_at"] += pd.Timedelta(days=20)
        pred = np.tile([950.0, 1430.0], (4, 1))
        corrected, trace = replay_personalization(frame, pred, "mean3")
        self.assertAlmostEqual(trace.loc[1, "gap_days"], 20 + 14/24)
        self.assertTrue((corrected[:, 0] <= 960).all())
        self.assertTrue((corrected[:, 1] <= 1440).all())
        self.assertTrue((corrected[:, 1] - corrected[:, 0] >= 30).all())

    def test_weighted_median_and_context_policies_run(self):
        frame = make_frame(9)
        pred = np.tile([90.0, 590.0], (9, 1))
        for policy in ("median3", "median7", "mean3_long", "context3", "fixed14", "none"):
            corrected, trace = replay_personalization(frame, pred, policy)
            self.assertEqual(corrected.shape, (9, 2))
            self.assertTrue(np.isfinite(corrected).all())
            self.assertEqual(set(trace.policy), {policy})
        self.assertFalse(replay_personalization(frame, pred, "none")[1].active.any())

    def test_policy_manifest_is_strict_json_safe(self):
        encoded = json.dumps(policy_specs(), allow_nan=False, sort_keys=True)
        self.assertIn('"fixed14"', encoded)
        self.assertIn('"unbounded"', encoded)
        self.assertNotIn('"legacy14"', encoded)

    def test_rejects_invalid_inputs(self):
        frame = make_frame(3)
        pred = np.tile([90.0, 590.0], (3, 1))
        duplicate = frame.copy(); duplicate.loc[1, "row_id"] = duplicate.loc[0, "row_id"]
        with self.assertRaisesRegex(ValueError, "unique"):
            replay_personalization(duplicate, pred, "mean3")
        bad = frame.copy(); bad.loc[1, "bed_offset"] = math.inf
        with self.assertRaisesRegex(ValueError, "finite"):
            replay_personalization(bad, pred, "mean3")
        bad_time = frame.copy(); bad_time.loc[1, "observed_at"] = bad_time.loc[1, "wake"] - pd.Timedelta(hours=1)
        with self.assertRaisesRegex(ValueError, "must not precede"):
            replay_personalization(bad_time, pred, "mean3")
        with self.assertRaisesRegex(ValueError, "unknown policy"):
            replay_personalization(frame, pred, "bogus")


if __name__ == "__main__":
    unittest.main()
