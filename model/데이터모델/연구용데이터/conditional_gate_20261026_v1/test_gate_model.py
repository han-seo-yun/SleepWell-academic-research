from __future__ import annotations

import json
import unittest
from unittest import mock

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from gate_model import (
    CONTEXT_COLUMNS,
    GATE_GRID,
    ConditionalGate,
    best_fixed_weight,
    make_context,
)


def valid_context(signal: list[float] | np.ndarray) -> pd.DataFrame:
    signal_array = np.asarray(signal, dtype=float)
    rows = len(signal_array)
    return pd.DataFrame(
        {
            "history_count_7": signal_array,
            "history_age_days": np.full(rows, 2.0),
            "recent_count14": np.full(rows, 3.0),
            "bed_mad": np.full(rows, 4.0),
            "wake_mad": np.full(rows, 5.0),
            "gap_days_mean": np.full(rows, 6.0),
            "personal_weight": np.full(rows, 0.75),
            "missing_fraction": np.zeros(rows),
            "disagreement_bed": np.full(rows, 4.0),
            "disagreement_wake": np.full(rows, 8.0),
        },
        columns=CONTEXT_COLUMNS,
    )


def split_training_data() -> tuple[
    pd.DataFrame, np.ndarray, np.ndarray, np.ndarray, np.ndarray
]:
    context = valid_context([0.0] * 4 + [10.0] * 4)
    data = np.tile(np.array([10.0, 100.0]), (8, 1))
    physical = data + np.array([4.0, 8.0])
    labels = data.copy()
    labels[4:] = physical[4:]
    sample_weight = np.ones(8)
    return context, data, physical, labels, sample_weight


class ContextContractTests(unittest.TestCase):
    def test_make_context_has_exact_safe_columns_and_requested_missing_fraction(self):
        features = pd.DataFrame(
            {
                "history_count_7": [1, 2, 3],
                "history_age_days": [2.0, np.nan, 4.0],
                "recent_count14": [3, 4, 5],
                "bed_mad": [4.0, 5.0, 6.0],
                "wake_mad": [5.0, 6.0, 7.0],
                "gap_days_mean": [6.0, 7.0, 8.0],
                "personal_weight": [0.5, 0.6, 0.7],
                "data_a": [np.nan, 1.0, np.nan],
                "data_b": [2.0, 3.0, np.nan],
                "unrequested_missing": [np.nan, np.nan, np.nan],
                "participant_id": ["p1", "p2", "p3"],
                "target": [100.0, 200.0, 300.0],
            },
            index=[11, 12, 13],
        )
        data = np.array([[1.0, 10.0], [2.0, 20.0], [3.0, 30.0]])
        physical = np.array([[4.0, 16.0], [2.0, 18.0], [1.0, 30.0]])
        feature_names = ["data_a", "data_b"]
        features_before = features.copy(deep=True)
        data_before = data.copy()
        physical_before = physical.copy()
        names_before = feature_names.copy()

        context = make_context(features, data, physical, feature_names)

        self.assertEqual(tuple(context.columns), CONTEXT_COLUMNS)
        self.assertEqual(context.index.tolist(), [11, 12, 13])
        np.testing.assert_allclose(
            context["missing_fraction"].to_numpy(), [0.5, 0.0, 1.0]
        )
        np.testing.assert_allclose(
            context["disagreement_bed"].to_numpy(), [3.0, 0.0, 2.0]
        )
        np.testing.assert_allclose(
            context["disagreement_wake"].to_numpy(), [6.0, 2.0, 0.0]
        )
        self.assertNotIn("participant_id", context)
        self.assertNotIn("target", context)
        assert_frame_equal(features, features_before)
        np.testing.assert_array_equal(data, data_before)
        np.testing.assert_array_equal(physical, physical_before)
        self.assertEqual(feature_names, names_before)

    def test_context_contract_mismatch_is_rejected_in_fit_and_predict(self):
        context, data, physical, labels, weights = split_training_data()
        gate = ConditionalGate(depth=1, min_samples_leaf=1).fit(
            context, data, physical, labels, weights
        )
        reordered = context.loc[:, list(reversed(CONTEXT_COLUMNS))]
        missing = context.drop(columns=["history_age_days"])

        with self.assertRaisesRegex(ValueError, "exactly match"):
            ConditionalGate(depth=1).fit(
                reordered, data, physical, labels, weights
            )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            gate.predict_weights(missing)

    def test_make_context_rejects_missing_source_columns_and_nonfinite_predictions(self):
        features = pd.DataFrame(
            {
                column: [1.0, 2.0]
                for column in CONTEXT_COLUMNS[:7]
            }
        )
        data = np.zeros((2, 2))
        physical = np.ones((2, 2))

        with self.assertRaisesRegex(ValueError, "missing required columns"):
            make_context(features, data, physical, ["model_input"])
        features["model_input"] = [1.0, 2.0]
        bad_data = data.copy()
        bad_data[0, 0] = np.nan
        with self.assertRaisesRegex(ValueError, "finite"):
            make_context(features, bad_data, physical, ["model_input"])


class FixedWeightTests(unittest.TestCase):
    def test_exact_zero_is_kept_when_physics_hurts(self):
        data = np.array([[0.0, 10.0], [2.0, 12.0], [4.0, 14.0]])
        physical = data + np.array([50.0, -40.0])
        labels = data.copy()

        weight = best_fixed_weight(data, physical, labels, np.ones(3))

        self.assertEqual(weight, 0.0)

    def test_positive_physics_correction_is_selected(self):
        data = np.zeros((4, 2))
        physical = np.tile([20.0, 40.0], (4, 1))
        labels = data + 0.5 * (physical - data)

        weight = best_fixed_weight(data, physical, labels, np.ones(4))

        self.assertEqual(weight, 0.5)

    def test_grid_ties_choose_the_lower_physics_weight(self):
        data = np.zeros((1, 2))
        physical = np.full((1, 2), 4.0)
        labels = np.full((1, 2), 1.5)

        # g=.25 and g=.5 have equal error; ascending-grid argmin must choose .25.
        self.assertEqual(
            best_fixed_weight(data, physical, labels, np.ones(1)), 0.25
        )

    def test_identical_predictions_choose_literal_zero(self):
        data = np.array([[1.0, 2.0], [3.0, 4.0]])
        labels = np.array([[5.0, 6.0], [7.0, 8.0]])

        self.assertEqual(
            best_fixed_weight(data, data.copy(), labels, np.ones(2)), 0.0
        )


class ConditionalGateTests(unittest.TestCase):
    def test_gate_learns_zero_and_useful_leaf_corrections_with_bounded_weights(self):
        context, data, physical, labels, sample_weight = split_training_data()
        gate = ConditionalGate(depth=1, min_samples_leaf=1).fit(
            context, data, physical, labels, sample_weight
        )

        learned = gate.predict_weights(context)
        prediction = gate.predict(context, data, physical)

        np.testing.assert_array_equal(learned[:4], np.zeros(4))
        np.testing.assert_array_equal(learned[4:], np.ones(4))
        self.assertTrue(np.isfinite(learned).all())
        self.assertTrue(np.all((learned >= 0.0) & (learned <= 1.0)))
        np.testing.assert_allclose(prediction, labels)

    def test_one_coefficient_is_shared_by_bed_and_wake(self):
        context = valid_context([0.0, 0.0, 0.0, 0.0])
        data = np.tile([10.0, 100.0], (4, 1))
        physical = data + np.array([20.0, 40.0])
        labels = data + 0.5 * (physical - data)
        gate = ConditionalGate(depth=1, min_samples_leaf=1).fit(
            context, data, physical, labels, np.ones(4)
        )

        weights = gate.predict_weights(context)
        prediction = gate.predict(context, data, physical)
        inferred_endpoint_weights = (prediction - data) / (physical - data)

        np.testing.assert_array_equal(weights, np.full(4, 0.5))
        np.testing.assert_allclose(
            inferred_endpoint_weights, weights[:, None] * np.ones((1, 2))
        )

    def test_leaf_weight_reoptimizes_actual_joint_mae_instead_of_tree_mean(self):
        context = valid_context([0.0, 0.0, 0.0])
        data = np.zeros((3, 2))
        physical = np.full((3, 2), 10.0)
        labels = np.array([[0.0, 0.0], [0.0, 0.0], [10.0, 10.0]])
        gate = ConditionalGate(depth=1, min_samples_leaf=1).fit(
            context, data, physical, labels, np.ones(3)
        )
        imputed = gate.imputer_.transform(context.to_numpy(dtype=float))

        np.testing.assert_allclose(gate.tree_.predict(imputed), np.full(3, 1.0 / 3.0))
        np.testing.assert_array_equal(gate.predict_weights(context), np.zeros(3))

    def test_identical_base_predictions_remain_unchanged_with_zero_weights(self):
        context = valid_context([0.0, 1.0, 2.0])
        predictions = np.array([[1.0, 11.0], [2.0, 12.0], [3.0, 13.0]])
        labels = predictions + 100.0
        gate = ConditionalGate(depth=2, min_samples_leaf=1).fit(
            context,
            predictions,
            predictions.copy(),
            labels,
            np.ones(3),
        )

        np.testing.assert_array_equal(gate.predict_weights(context), np.zeros(3))
        np.testing.assert_array_equal(
            gate.predict(context, predictions, predictions.copy()), predictions
        )

    def test_predict_uses_training_imputer_transform_without_refitting(self):
        context, data, physical, labels, sample_weight = split_training_data()
        context.loc[:, "history_age_days"] = np.nan
        gate = ConditionalGate(depth=1, min_samples_leaf=1).fit(
            context, data, physical, labels, sample_weight
        )
        statistics_before = gate.imputer_.statistics_.copy()
        inference_context = valid_context([0.0, 10.0])
        inference_context.loc[0, "bed_mad"] = np.nan

        with mock.patch.object(
            gate.imputer_, "fit", side_effect=AssertionError("unexpected fit")
        ), mock.patch.object(
            gate.imputer_,
            "fit_transform",
            side_effect=AssertionError("unexpected fit_transform"),
        ), mock.patch.object(
            gate.imputer_, "transform", wraps=gate.imputer_.transform
        ) as transform:
            weights = gate.predict_weights(inference_context)

        transform.assert_called_once()
        np.testing.assert_array_equal(gate.imputer_.statistics_, statistics_before)
        self.assertEqual(weights.shape, (2,))

    def test_fit_and_predict_do_not_mutate_inputs(self):
        context, data, physical, labels, sample_weight = split_training_data()
        context.loc[1, "bed_mad"] = np.nan
        originals = (
            context.copy(deep=True),
            data.copy(),
            physical.copy(),
            labels.copy(),
            sample_weight.copy(),
        )
        gate = ConditionalGate(depth=1, min_samples_leaf=1).fit(
            context, data, physical, labels, sample_weight
        )
        gate.predict(context, data, physical)

        assert_frame_equal(context, originals[0])
        for actual, expected in zip(
            (data, physical, labels, sample_weight), originals[1:]
        ):
            np.testing.assert_array_equal(actual, expected)

    def test_description_and_outputs_are_deterministic_and_json_serializable(self):
        context, data, physical, labels, sample_weight = split_training_data()
        first = ConditionalGate(depth=1, min_samples_leaf=1, seed=20261026).fit(
            context, data, physical, labels, sample_weight
        )
        second = ConditionalGate(depth=1, min_samples_leaf=1, seed=20261026).fit(
            context, data, physical, labels, sample_weight
        )

        np.testing.assert_array_equal(
            first.predict_weights(context), second.predict_weights(context)
        )
        self.assertEqual(first.description(), second.description())
        description = first.description()
        json.dumps(description, allow_nan=False)
        self.assertEqual(description["feature_names"], list(CONTEXT_COLUMNS))
        self.assertEqual(description["grid"], GATE_GRID.tolist())
        self.assertEqual(description["depth"], 1)
        self.assertIn("history_count_7", description["tree_rules"])
        self.assertEqual(set(description["leaf_weights"].values()), {0.0, 1.0})


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context, self.data, self.physical, self.labels, self.weights = (
            split_training_data()
        )

    def test_wrong_prediction_and_label_shapes_are_rejected(self):
        bad_inputs = {
            "one-dimensional data": (
                self.data[:, 0],
                self.physical,
                self.labels,
            ),
            "one-endpoint physics": (
                self.data,
                self.physical[:, :1],
                self.labels,
            ),
            "three-endpoint labels": (
                self.data,
                self.physical,
                np.column_stack([self.labels, np.ones(len(self.labels))]),
            ),
            "row mismatch": (
                self.data[:-1],
                self.physical,
                self.labels,
            ),
        }
        for name, (data, physical, labels) in bad_inputs.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                best_fixed_weight(data, physical, labels, self.weights)

    def test_nonfinite_predictions_and_labels_are_rejected(self):
        cases: list[tuple[str, np.ndarray, np.ndarray, np.ndarray]] = []
        bad_data = self.data.copy()
        bad_data[0, 0] = np.nan
        cases.append(("nan data", bad_data, self.physical, self.labels))
        bad_physical = self.physical.copy()
        bad_physical[0, 1] = np.inf
        cases.append(("infinite physics", self.data, bad_physical, self.labels))
        bad_labels = self.labels.copy()
        bad_labels[0, 0] = -np.inf
        cases.append(("infinite label", self.data, self.physical, bad_labels))

        for name, data, physical, labels in cases:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "finite"):
                best_fixed_weight(data, physical, labels, self.weights)

    def test_invalid_sample_weights_are_rejected(self):
        invalid = {
            "zero": np.array([0.0] + [1.0] * 7),
            "negative": np.array([-1.0] + [1.0] * 7),
            "nan": np.array([np.nan] + [1.0] * 7),
            "infinite": np.array([np.inf] + [1.0] * 7),
            "wrong length": np.ones(7),
            "not one-dimensional": np.ones((8, 1)),
        }
        for name, sample_weight in invalid.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                best_fixed_weight(
                    self.data,
                    self.physical,
                    self.labels,
                    sample_weight,
                )

    def test_fit_and_predict_reject_row_mismatches_and_infinite_context(self):
        with self.assertRaisesRegex(ValueError, "rows"):
            ConditionalGate(depth=1).fit(
                self.context.iloc[:-1],
                self.data,
                self.physical,
                self.labels,
                self.weights,
            )

        gate = ConditionalGate(depth=1, min_samples_leaf=1).fit(
            self.context,
            self.data,
            self.physical,
            self.labels,
            self.weights,
        )
        with self.assertRaisesRegex(ValueError, "rows"):
            gate.predict(self.context, self.data[:-1], self.physical)
        infinite_context = self.context.copy()
        infinite_context.loc[0, "bed_mad"] = np.inf
        with self.assertRaisesRegex(ValueError, "infinite"):
            gate.predict_weights(infinite_context)


if __name__ == "__main__":
    unittest.main()
