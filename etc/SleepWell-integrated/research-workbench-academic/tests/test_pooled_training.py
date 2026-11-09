from __future__ import annotations

import inspect
import unittest

import numpy as np
import pandas as pd

from researchbench import pooled_training as pooled
from researchbench.academic_backend import AcademicRunCancelled


def episode(
    cohort: str,
    subject_id: str,
    cutoff: str,
    bed: str,
    wake: str,
    bedtime_min: float,
    wake_min: float,
) -> dict:
    return {
        "cohort": cohort,
        "subject_id": subject_id,
        "split": "train",
        "cutoff": cutoff,
        "bed": bed,
        "wake": wake,
        "observed_at": wake,
        "bedtime_min": bedtime_min,
        "wake_min": wake_min,
    }


class PooledTrainingTests(unittest.TestCase):
    def test_balanced_weights_give_equal_cohort_and_participant_mass(self):
        frame = pd.DataFrame([
            {"cohort": "CMI", "subject_id": "a"},
            {"cohort": "CMI", "subject_id": "a"},
            {"cohort": "CMI", "subject_id": "b"},
            {"cohort": "MESA", "subject_id": "a"},
            {"cohort": "MESA", "subject_id": "a"},
            {"cohort": "MESA", "subject_id": "a"},
            {"cohort": "MESA", "subject_id": "b"},
            {"cohort": "MESA", "subject_id": "c"},
        ])
        frame["weight"] = pooled.balanced_weights(frame)

        cohort_mass = frame.groupby("cohort").weight.sum()
        participant_mass = frame.groupby(["cohort", "subject_id"]).weight.sum()
        self.assertAlmostEqual(cohort_mass["CMI"], cohort_mass["MESA"])
        self.assertAlmostEqual(participant_mass["CMI"].max(), participant_mass["CMI"].min())
        self.assertAlmostEqual(participant_mass["MESA"].max(), participant_mass["MESA"].min())
        self.assertAlmostEqual(float(frame.weight.mean()), 1.0)

    def test_prior_is_equal_cohort_then_equal_participant_and_train_only(self):
        frame = pd.DataFrame([
            {"cohort": "CMI", "subject_id": "a", "split": "train", "bedtime_min": 100, "wake_min": 500},
            {"cohort": "CMI", "subject_id": "a", "split": "train", "bedtime_min": 200, "wake_min": 600},
            {"cohort": "CMI", "subject_id": "b", "split": "train", "bedtime_min": 300, "wake_min": 700},
            {"cohort": "MESA", "subject_id": "a", "split": "train", "bedtime_min": 500, "wake_min": 900},
        ])
        prior = pooled.prior_from_train(frame)
        # CMI participant mean is ((150 + 300) / 2), MESA is 500; cohorts are equal.
        self.assertAlmostEqual(prior["bed_offset"], 362.5)
        self.assertAlmostEqual(prior["wake_offset"], 762.5)
        self.assertEqual(prior["n_people"], 3)
        self.assertEqual(prior["n_nights"], 4)

        invalid = pd.concat([frame, frame.iloc[[0]].assign(split="validation")], ignore_index=True)
        with self.assertRaisesRegex(ValueError, "training participants only"):
            pooled.prior_from_train(invalid)

    def test_causal_features_do_not_read_current_target_but_use_past_night(self):
        prior = {"bed_offset": 180.0, "wake_offset": 660.0}
        rows = [
            episode("CMI", "person", "2026-01-01T20:00:00+09:00",
                    "2026-01-01T23:00:00+09:00", "2026-01-02T07:00:00+09:00", 180, 660),
            episode("CMI", "person", "2026-01-02T20:00:00+09:00",
                    "2026-01-02T23:30:00+09:00", "2026-01-03T07:30:00+09:00", 210, 690),
        ]
        base, _, counts = pooled.replay_features(pd.DataFrame(rows), prior)
        self.assertEqual(counts, [0, 1])

        current_changed = pd.DataFrame(rows)
        current_changed.loc[1, ["bedtime_min", "wake_min"]] = [400, 1000]
        current_features, _, _ = pooled.replay_features(current_changed, prior)
        np.testing.assert_allclose(base.iloc[1], current_features.iloc[1], rtol=0, atol=0)

        past_changed = pd.DataFrame(rows)
        past_changed.loc[0, ["bed", "wake", "observed_at", "bedtime_min", "wake_min"]] = [
            "2026-01-02T00:00:00+09:00", "2026-01-02T08:00:00+09:00",
            "2026-01-02T08:00:00+09:00", 240, 720,
        ]
        past_features, _, _ = pooled.replay_features(past_changed, prior)
        self.assertFalse(np.array_equal(base.iloc[1].to_numpy(), past_features.iloc[1].to_numpy()))

    def test_same_subject_id_in_different_cohorts_has_separate_history(self):
        prior = {"bed_offset": 180.0, "wake_offset": 660.0}
        frame = pd.DataFrame([
            episode("CMI", "shared", "2026-01-01T20:00:00+09:00",
                    "2026-01-01T23:00:00+09:00", "2026-01-02T07:00:00+09:00", 180, 660),
            episode("CMI", "shared", "2026-01-02T20:00:00+09:00",
                    "2026-01-02T23:00:00+09:00", "2026-01-03T07:00:00+09:00", 180, 660),
            episode("MESA", "shared", "2026-01-02T20:00:00+09:00",
                    "2026-01-02T23:00:00+09:00", "2026-01-03T07:00:00+09:00", 180, 660),
        ])
        _, _, counts = pooled.replay_features(frame, prior)
        self.assertEqual(counts, [0, 1, 0])

    def test_validation_score_weights_participants_then_cohorts(self):
        frame = pd.DataFrame([
            {"cohort": "CMI", "subject_id": "a", "bedtime_min": 0, "wake_min": 0},
            {"cohort": "CMI", "subject_id": "b", "bedtime_min": 0, "wake_min": 0},
            {"cohort": "CMI", "subject_id": "b", "bedtime_min": 0, "wake_min": 0},
            {"cohort": "CMI", "subject_id": "b", "bedtime_min": 0, "wake_min": 0},
            {"cohort": "MESA", "subject_id": "a", "bedtime_min": 0, "wake_min": 0},
        ])
        predictions = np.array([[0, 0], [10, 10], [10, 10], [10, 10], [20, 20]], float)
        # CMI participant mean: (0 + 10) / 2 = 5; MESA participant mean: 20.
        self.assertAlmostEqual(pooled.validation_score(frame, predictions), 12.5)
        people = pooled.participant_errors(frame, predictions)
        self.assertEqual(len(people), 3)
        self.assertIn(("CMI", "a"), people.index)
        self.assertIn(("MESA", "a"), people.index)

    def test_identical_models_have_zero_paired_difference_and_interval(self):
        index = pd.MultiIndex.from_tuples(
            [("CMI", "a"), ("CMI", "b"), ("MESA", "a"), ("MESA", "b")],
            names=["cohort", "subject_id"],
        )
        values = pd.DataFrame({"joint_mae_min": [1.0, 3.0, 2.0, 4.0]}, index=index)
        models = ("pooled_data", "existing_data", "existing_hybrid", "recent_median")
        errors = {name: values.copy() for name in models}
        bootstraps = {
            name: {cohort: pooled.bootstrap_means(values.xs(cohort).joint_mae_min, 200, 17 + number)
                   for number, cohort in enumerate(pooled.COHORTS)}
            for name in models
        }
        comparisons = pooled.paired_comparisons(errors, bootstraps, "pooled_data")
        self.assertEqual(len(comparisons), 9)
        for row in comparisons:
            self.assertEqual(row["improvement_min"], 0.0)
            self.assertEqual(row["ci95_low"], 0.0)
            self.assertEqual(row["ci95_high"], 0.0)

    def test_preprocessor_limits_are_learned_only_from_fit_rows(self):
        model = pooled.make_model("ridge100", seed=5)
        train_x = np.array([[0.0, 10.0], [1.0, 20.0], [2.0, 30.0]])
        train_y = np.array([[100.0, 500.0], [110.0, 510.0], [120.0, 520.0]])
        model.fit(train_x, train_y, model__sample_weight=np.ones(3))
        clip = model.named_steps["clip"]
        np.testing.assert_allclose(clip.low_, [0.02, 10.2])
        np.testing.assert_allclose(clip.high_, [1.98, 29.8])

        # A later extreme validation row is transformed with training limits and cannot alter them.
        before_low, before_high = clip.low_.copy(), clip.high_.copy()
        model.predict(np.array([[1_000_000.0, -1_000_000.0]]))
        np.testing.assert_array_equal(clip.low_, before_low)
        np.testing.assert_array_equal(clip.high_, before_high)

    def test_selection_is_locked_before_test_and_checkpoint_cancels(self):
        source = inspect.getsource(pooled.run_pooled_retraining)
        lock_write = source.index('write_json(output / "selection_lock.json", lock)')
        test_materialization = source.index('test = frame.loc[frame.split.eq("test")]')
        self.assertLess(lock_write, test_materialization)
        self.assertIn('"test_used_for_selection": False', source[:test_materialization])

        with self.assertRaises(AcademicRunCancelled):
            pooled.checkpoint(None, lambda: True, "before_fit", 0.0)


if __name__ == "__main__":
    unittest.main()
