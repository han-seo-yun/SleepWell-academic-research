from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

from researchbench import controlled_comparison as controlled
from researchbench.academic_backend import AcademicRunCancelled


class _RecordingEstimator:
    def __init__(self, calls):
        self.calls = calls

    def fit(self, X, y, sample_weight=None):
        self.calls.append({
            "X": np.asarray(X, float).copy(),
            "y": np.asarray(y, float).copy(),
            "weights": np.asarray(sample_weight, float).copy(),
        })
        self.center = np.asarray(y, float).mean(axis=0)
        return self

    def predict(self, X):
        return np.tile(self.center, (len(X), 1))


class ControlledComparisonTests(unittest.TestCase):
    def test_weighted_quantile_allows_each_person_equal_total_influence(self):
        # One participant contributes four nights at 0; one contributes one night at 100.
        values = np.array([0.0, 0.0, 0.0, 0.0, 100.0])
        row_equal = np.ones(5)
        participant_equal = np.array([0.25, 0.25, 0.25, 0.25, 1.0])

        self.assertEqual(controlled.weighted_quantile(values, 0.5, row_equal), 0.0)
        # Inverse empirical CDF uses the left value at the exact 50% boundary.
        self.assertEqual(controlled.weighted_quantile(values, 0.5, participant_equal), 0.0)
        self.assertEqual(controlled.weighted_quantile(values, 0.500001, participant_equal), 100.0)

    def test_preprocessor_is_fit_only_on_training_numeric_columns(self):
        training = np.array([
            [0.0, 10.0],
            [1.0, np.nan],
            [100.0, 30.0],
        ])
        weights = np.array([1.0, 1.0, 0.25])
        preprocessor = controlled.WeightedPreprocessor(clip_quantile=0.1).fit(
            training, sample_weight=weights,
        )
        state = tuple(value.copy() for value in (
            preprocessor.median_, preprocessor.low_, preprocessor.high_,
            preprocessor.mean_, preprocessor.scale_, preprocessor.keep_,
        ))

        transformed = preprocessor.transform(np.array([[1_000_000.0, -1_000_000.0]]))
        self.assertTrue(np.isfinite(transformed).all())
        for before, after in zip(state, (
            preprocessor.median_, preprocessor.low_, preprocessor.high_,
            preprocessor.mean_, preprocessor.scale_, preprocessor.keep_,
        )):
            np.testing.assert_array_equal(before, after)

        encoded = controlled.encode_features(
            np.vstack([transformed, transformed]), np.array(["CMI", "MESA"]), include_source=True,
        )
        np.testing.assert_array_equal(encoded[:, -2:], [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(encoded.shape[1], transformed.shape[1] + 2)

    def test_participant_folds_namespace_ids_and_cover_each_person_once_per_repeat(self):
        rows = []
        for cohort in controlled.pt.COHORTS:
            for person in ("shared", "p1", "p2", "p3", "p4", "p5"):
                for night in range(1 + (person == "shared")):
                    rows.append({"cohort": cohort, "subject_id": person,
                                 "split": "train", "night": night})
        frame = pd.DataFrame(rows, index=np.arange(100, 100 + len(rows)))
        first = list(controlled.participant_folds(frame, folds=3, repeats=2, seed=11))
        second = list(controlled.participant_folds(frame, folds=3, repeats=2, seed=11))
        self.assertEqual([(r, f, list(a), list(b)) for r, f, a, b in first],
                         [(r, f, list(a), list(b)) for r, f, a, b in second])

        expected = set(map(tuple, frame[["cohort", "subject_id"]].drop_duplicates().to_numpy()))
        held_by_repeat = {0: [], 1: []}
        for repeat, _, fit_indices, held_indices in first:
            fit_people = set(map(tuple, frame.loc[fit_indices, ["cohort", "subject_id"]]
                                 .drop_duplicates().to_numpy()))
            held_people = set(map(tuple, frame.loc[held_indices, ["cohort", "subject_id"]]
                                  .drop_duplicates().to_numpy()))
            self.assertFalse(fit_people & held_people)
            self.assertEqual(set(frame.loc[held_indices, "cohort"]), set(controlled.pt.COHORTS))
            held_by_repeat[repeat].extend(held_people)
        for held in held_by_repeat.values():
            self.assertEqual(set(held), expected)
            self.assertEqual(len(held), len(expected))

    def test_fit_strategy_preserves_separate_shared_and_pooled_boundaries(self):
        fit = pd.DataFrame([
            {"cohort": "CMI", "subject_id": "c", "bedtime_min": 100, "wake_min": 500},
            {"cohort": "CMI", "subject_id": "c", "bedtime_min": 110, "wake_min": 510},
            {"cohort": "MESA", "subject_id": "m", "bedtime_min": 200, "wake_min": 600},
            {"cohort": "MESA", "subject_id": "m", "bedtime_min": 210, "wake_min": 610},
        ])
        evaluation = pd.DataFrame([
            {"cohort": "CMI", "subject_id": "x", "bedtime_min": 120, "wake_min": 520},
            {"cohort": "MESA", "subject_id": "y", "bedtime_min": 220, "wake_min": 620},
        ])
        views = {
            "shared_prior": {"bed_offset": 150, "wake_offset": 550},
            "separate_prior": {"CMI": {}, "MESA": {}},
        }
        prepared = {
            "names": ["feature"], "weights": np.ones(4),
            "shared_fit": np.array([[10.0], [11.0], [20.0], [21.0]]),
            "shared_eval": np.array([[12.0], [22.0]]),
            "separate_fit": {"CMI": np.array([[1.0], [2.0]]),
                             "MESA": np.array([[3.0], [4.0]])},
            "separate_eval": {"CMI": np.array([[1.5]]), "MESA": np.array([[3.5]])},
            "separate": {"CMI": object(), "MESA": object()},
            "shared": object(),
        }

        def execute(strategy):
            calls = []
            wrapper = lambda *args, **kwargs: SimpleNamespace(
                named_steps={"model": _RecordingEstimator(calls)})
            core = SimpleNamespace(constrain=lambda bed, wake: (float(bed), float(wake)),
                                   GRADUAL_POLICY="gradual_v1")
            with mock.patch.object(controlled.pt, "make_model", side_effect=wrapper), \
                    mock.patch.object(controlled.pt, "vendor_module", return_value=core):
                bundle, prediction, converged = controlled.fit_strategy(
                    strategy, fit, evaluation, views, prepared, "ridge100", 7,
                )
            self.assertTrue(converged)
            self.assertEqual(prediction.shape, (2, 2))
            return calls, bundle

        separate_calls, separate = execute("separate")
        self.assertEqual(set(separate["models"]), set(controlled.pt.COHORTS))
        np.testing.assert_array_equal(separate_calls[0]["X"], [[1.0], [2.0]])
        np.testing.assert_array_equal(separate_calls[1]["X"], [[3.0], [4.0]])

        shared_calls, shared = execute("separate_shared")
        self.assertEqual(set(shared["models"]), set(controlled.pt.COHORTS))
        np.testing.assert_array_equal(shared_calls[0]["X"], [[10.0], [11.0]])
        np.testing.assert_array_equal(shared_calls[1]["X"], [[20.0], [21.0]])

        pooled_calls, pooled = execute("pooled")
        self.assertEqual(set(pooled["models"]), {"ALL"})
        np.testing.assert_array_equal(pooled_calls[0]["X"], [[10.0], [11.0], [20.0], [21.0]])

        source_calls, source = execute("pooled_source")
        self.assertEqual(source["source_encoding"], list(controlled.pt.COHORTS))
        self.assertEqual(source_calls[0]["X"].shape, (4, 3))
        np.testing.assert_array_equal(source_calls[0]["X"][:, -2:],
                                      [[1, 0], [1, 0], [0, 1], [0, 1]])

    def test_common_config_uses_only_data_separate_and_pooled_cv(self):
        rows = []
        for preprocessing, candidate, base in (
            ("none", "ridge100", 10.0),
            ("weighted_01_99", "absolute7", 5.0),
        ):
            for repeat in range(2):
                for fold in range(2):
                    for strategy in controlled.STRATEGIES:
                        for descriptors in controlled.DESCRIPTORS:
                            score = base
                            if descriptors == "hybrid" or strategy == "pooled_source":
                                score = 0.01  # Deliberately tempting but outside primary selection.
                            rows.append({
                                "repeat": repeat, "fold": fold, "strategy": strategy,
                                "descriptors": descriptors, "preprocessing": preprocessing,
                                "candidate": candidate, "macro_joint_mae_min": score,
                                "converged": True,
                            })
        selected = controlled.select_common_config(rows)
        self.assertEqual(selected["preprocessing"], "weighted_01_99")
        self.assertEqual(selected["candidate"], "absolute7")
        self.assertEqual(selected["cv_balanced_strategy_macro_mae_min"], 5.0)
        self.assertIn("same configuration for all eight models", selected["criterion"])

    def test_configuration_and_artifacts_lock_before_test_materialization(self):
        source = inspect.getsource(controlled.run_controlled_comparison)
        configuration_lock = source.index("output/'configuration_lock.json'")
        artifact_lock = source.index("output/'selection_lock.json'")
        test_materialization = source.index("test = frame.loc[frame.split.eq('test')]")
        self.assertLess(configuration_lock, artifact_lock)
        self.assertLess(artifact_lock, test_materialization)
        before_test = source[:test_materialization]
        self.assertIn("validation_used_for_selection': False", before_test)
        self.assertNotIn("test_summary", before_test)
        # The final eight models reuse one CV-selected preprocessing/candidate pair.
        self.assertIn("prepare_setting(train, validation, views, descriptors, common['preprocessing'])",
                      before_test)
        self.assertIn("views, prepared, common['candidate'], seed", before_test)

    def test_controlled_run_honors_cancellation_before_database_read_or_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "result"
            config = {
                "database_path": str(root / "missing.sqlite3"),
                "output_dir": str(output),
                "cv_folds": 3, "cv_repeats": 2, "bootstrap_replicates": 100,
            }
            with self.assertRaises(AcademicRunCancelled):
                controlled.run_controlled_comparison(config, cancelled=lambda: True)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
