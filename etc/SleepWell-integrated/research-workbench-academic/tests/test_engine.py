from __future__ import annotations

import unittest
from copy import deepcopy

import numpy as np
import pandas as pd

from researchbench.engine import (
    ExperimentCancelled,
    _feature_frame,
    _preprocessor,
    evaluate_final,
    make_split_plan,
    run_experiment,
    validate_split_plan,
)
from researchbench.metrics import classification_metrics, regression_metrics


class SplitPlanTests(unittest.TestCase):
    def test_explicit_none_disables_stratification_without_dropping_rows(self):
        df = pd.DataFrame({"x": range(30), "target": [0, 1] * 15})
        config = {
            "task": "classification", "target": "target", "features": ["x"],
            "split": "random", "folds": 3, "stratify_target": None, "seed": 2,
        }
        plan = make_split_plan(df, config)
        self.assertEqual(len(plan["train_rows"]) + len(plan["test_rows"]), len(df))

    def test_group_split_never_crosses_subjects(self):
        n_groups = 12
        df = pd.DataFrame({
            "x": np.arange(n_groups * 3),
            "subject": np.repeat(np.arange(n_groups), 3),
            "target": np.tile([0, 1, 0], n_groups),
        })
        config = {
            "task": "classification", "target": "target", "features": ["x"],
            "split": "group", "group": "subject", "folds": 3, "seed": 7,
        }
        plan = make_split_plan(df, config)
        self.assertTrue(set(df.iloc[plan["train_rows"]].subject).isdisjoint(df.iloc[plan["test_rows"]].subject))
        for fold in plan["folds"]:
            train_groups = set(df.iloc[fold["train"]].subject)
            valid_groups = set(df.iloc[fold["valid"]].subject)
            self.assertTrue(train_groups.isdisjoint(valid_groups))

    def test_time_split_preserves_order(self):
        df = pd.DataFrame({
            "x": np.arange(40), "when": pd.date_range("2023-01-01", periods=40),
            "target": np.arange(40, dtype=float),
        })
        config = {
            "task": "regression", "target": "target", "features": ["x"],
            "split": "time", "time": "when", "folds": 3,
        }
        plan = make_split_plan(df, config)
        self.assertLess(max(plan["train_rows"]), min(plan["test_rows"]))
        for fold in plan["folds"]:
            self.assertLess(max(fold["train"]), min(fold["valid"]))

    def test_equal_timestamps_never_cross_time_boundaries(self):
        df = pd.DataFrame({
            "x": np.arange(60),
            "when": np.repeat(pd.date_range("2023-01-01", periods=20), 3),
            "target": np.arange(60, dtype=float),
        })
        config = {
            "task": "regression", "target": "target", "features": ["x"],
            "split": "time", "time": "when", "folds": 3,
        }
        plan = make_split_plan(df, config)
        self.assertTrue(set(df.iloc[plan["train_rows"]].when).isdisjoint(df.iloc[plan["test_rows"]].when))
        for fold in plan["folds"]:
            self.assertTrue(set(df.iloc[fold["train"]].when).isdisjoint(df.iloc[fold["valid"]].when))

    def test_shared_targets_use_union_and_each_run_records_effective_rows(self):
        df = pd.DataFrame({
            "x": np.arange(40),
            "class_target": [0, 1] * 15 + [np.nan] * 10,
            "reg_target": [np.nan] * 10 + list(np.arange(30, dtype=float)),
        })
        config = {
            "task": "classification", "target": "class_target", "features": ["x"],
            "shared_targets": ["class_target", "reg_target"], "stratify_target": None,
            "split": "random", "folds": 3, "seed": 4, "model": "dummy",
        }
        plan = make_split_plan(df, config)
        self.assertEqual(len(plan["train_rows"]) + len(plan["test_rows"]), 40)
        result = run_experiment(df, config, plan)
        self.assertLess(sum(fold["n_valid"] for fold in result["folds"]), len(plan["train_rows"]))

    def test_mutated_or_overlapping_plan_is_rejected(self):
        df = pd.DataFrame({"x": range(40), "target": [0, 1] * 20})
        config = {
            "task": "classification", "target": "target", "features": ["x"],
            "split": "random", "folds": 3, "seed": 3,
        }
        plan = make_split_plan(df, config)
        corrupted = deepcopy(plan)
        corrupted["test_rows"][0] = corrupted["train_rows"][0]
        with self.assertRaisesRegex(ValueError, "overlap"):
            validate_split_plan(df, config, corrupted)
        mutated = deepcopy(plan)
        mutated["folds"][0]["valid"].reverse()
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_split_plan(df, config, mutated)


class LeakageAndEvaluationTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(11)
        n = 120
        x = rng.normal(size=n)
        cat = np.where(rng.random(n) > 0.5, "A", "B")
        self.classification = pd.DataFrame({
            "x": x, "noise": rng.normal(size=n), "cat": cat,
            "target": np.where(x + (cat == "A") * 0.5 > 0, "case", "control"),
        })
        self.classification.loc[[2, 7, 13], "x"] = np.nan
        self.regression = pd.DataFrame({
            "x": x, "noise": rng.normal(size=n), "cat": cat,
            "target": 10.0 + 4.0 * x + rng.normal(scale=0.25, size=n),
        })

    def test_imputer_is_fit_on_train_frame_only(self):
        train = pd.DataFrame({"x": [0.0, 2.0], "cat": ["a", "b"]})
        validation = pd.DataFrame({"x": [10000.0, np.nan], "cat": ["z", None]})
        config = {"imputer": "mean", "encoding": "onehot", "scaling": "none"}
        preprocessor = _preprocessor(train, config)
        preprocessor.fit(train)
        statistic = preprocessor.named_transformers_["numeric"].named_steps["imputer"].statistics_[0]
        self.assertEqual(statistic, 1.0)
        transformed = preprocessor.transform(validation)
        self.assertEqual(transformed[1, 0], 1.0)

    def test_classification_and_final_holdout_are_separate(self):
        config = {
            "id": "c1", "name": "classification", "task": "classification",
            "target": "target", "features": ["x", "cat"], "split": "random",
            "folds": 3, "seed": 19, "model": "linear", "positive_class": "case",
            "threshold": 0.6, "average": "macro", "imputer": "median",
            "encoding": "onehot", "scaling": "standard",
        }
        plan = make_split_plan(self.classification, config)
        validation = run_experiment(self.classification, config, plan)
        tested = evaluate_final(self.classification, config, plan)
        validation_rows = {p["row_id"] for p in validation["predictions"]}
        self.assertTrue(validation_rows.isdisjoint(plan["test_rows"]))
        self.assertEqual({p["row_id"] for p in tested["predictions"]}, set(plan["test_rows"]))
        self.assertEqual(validation["phase"], "validation")
        self.assertEqual(tested["phase"], "final_test")
        self.assertGreater(validation["summary"]["roc_auc"]["mean"], 0.8)

    def test_feature_comparisons_reuse_exact_split(self):
        full = {
            "id": "full", "task": "regression", "target": "target",
            "features": ["x", "noise", "cat"], "split": "random", "folds": 4,
            "seed": 4, "model": "linear", "imputer": "median", "encoding": "onehot",
            "scaling": "standard", "target_transform": "none",
        }
        plan = make_split_plan(self.regression, full)
        reduced = dict(full, id="reduced", features=["x", "cat"])
        result_a = run_experiment(self.regression, full, plan)
        result_b = run_experiment(self.regression, reduced, plan)
        self.assertEqual(result_a["split_id"], result_b["split_id"])
        self.assertEqual(
            [f["valid_rows"] for f in result_a["folds"]],
            [f["valid_rows"] for f in result_b["folds"]],
        )
        self.assertGreater(result_a["summary"]["r2"]["mean"], 0.9)

    def test_log_target_predictions_are_returned_in_original_units(self):
        df = pd.DataFrame({"x": np.arange(60), "target": np.exp(np.linspace(1, 3, 60))})
        config = {
            "task": "regression", "target": "target", "features": ["x"],
            "split": "random", "folds": 3, "seed": 5, "model": "dummy",
            "imputer": "median", "encoding": "onehot", "scaling": "standard",
            "target_transform": "log1p",
        }
        plan = make_split_plan(df, config)
        result = run_experiment(df, config, plan)
        self.assertTrue(all(p["y_pred"] > 1.0 for p in result["predictions"]))
        self.assertLess(result["summary"]["mae"]["mean"], df.target.max())

    def test_standard_target_transform_is_fit_per_training_fold(self):
        df = pd.DataFrame({"x": np.arange(50), "target": np.arange(50, dtype=float)})
        df.loc[49, "target"] = 1_000_000.0
        config = {
            "task": "regression", "target": "target", "features": ["x"],
            "split": "time", "time": "x", "folds": 3, "seed": 42,
            "model": "dummy", "target_transform": "standard",
        }
        result = run_experiment(df, config, make_split_plan(df, config))
        for fold in result["folds"]:
            expected = float(df.iloc[fold["train_rows"]].target.mean())
            predictions = [p["y_pred"] for p in result["predictions"] if p["fold"] == fold["fold"]]
            self.assertTrue(all(abs(value - expected) < 1e-8 for value in predictions))

    def test_internal_cut_points_make_stable_json_safe_classes(self):
        df = pd.DataFrame({"x": np.arange(90), "target": np.linspace(-2, 4, 90)})
        config = {
            "task": "classification", "target": "target", "features": ["x"],
            "split": "random", "folds": 3, "seed": 6, "model": "dummy",
            "bin_edges": [0.0, 2.0], "bin_rationale": "clinical thresholds",
        }
        result = run_experiment(df, config, make_split_plan(df, config))
        self.assertEqual(result["classes"], ["≤ 0", "> 0–≤ 2", "> 2"])
        self.assertEqual(result["target_rule"]["effective_edges"], ["-inf", 0.0, 2.0, "inf"])
        self.assertEqual(result["target_rule"]["rationale"], "clinical thresholds")
        self.assertTrue(all(len(p["probabilities"]) == 3 for p in result["predictions"]))


class MetricAndRuntimeTests(unittest.TestCase):
    def test_non_default_positive_class_and_threshold(self):
        y = np.array(["yes", "no", "yes", "no"])
        classes = np.array(["yes", "no"], dtype=object)
        probabilities = np.array([[.7, .3], [.4, .6], [.55, .45], [.8, .2]])
        metrics, reasons, predicted = classification_metrics(
            y, probabilities, classes, positive_class="yes", threshold=.6
        )
        self.assertEqual(predicted.tolist(), ["yes", "no", "no", "yes"])
        self.assertAlmostEqual(metrics["recall"], .5)
        self.assertNotIn("roc_auc", reasons)

    def test_unavailable_metrics_are_none_with_reasons(self):
        metrics, reasons, _ = classification_metrics(
            np.array(["yes", "yes"]), np.array([[.8, .2], [.6, .4]]),
            np.array(["no", "yes"], dtype=object), positive_class="yes",
        )
        self.assertIsNone(metrics["roc_auc"])
        self.assertEqual(reasons["roc_auc"], "validation_has_one_class")
        no_positive, no_positive_reasons, _ = classification_metrics(
            np.array(["no", "no"]), np.array([[.9, .1], [.8, .2]]),
            np.array(["no", "yes"]), positive_class="yes",
        )
        self.assertIsNone(no_positive["average_precision"])
        self.assertEqual(no_positive_reasons["average_precision"], "validation_has_no_positive")
        regression, regression_reasons = regression_metrics([1.0], [1.2])
        self.assertIsNone(regression["r2"])
        self.assertEqual(regression_reasons["r2"], "requires_two_samples")

    def test_mlp_reports_epochs_and_honors_cancellation(self):
        rng = np.random.default_rng(3)
        df = pd.DataFrame({"x": rng.normal(size=80)})
        df["target"] = np.where(df.x > 0, "high", "low")
        config = {
            "task": "classification", "target": "target", "features": ["x"],
            "split": "random", "folds": 2, "seed": 2, "model": "mlp",
            "positive_class": "high", "imputer": "median", "encoding": "onehot",
            "scaling": "standard", "class_weight": "balanced",
            "params": {"epochs": 2, "hidden_layer_sizes": [4]},
        }
        plan = make_split_plan(df, config)
        events = []
        result = run_experiment(df, config, plan, progress=events.append)
        self.assertEqual(len(result["history"]), 4)
        self.assertTrue(any(event.get("epoch") == 2 for event in events))
        with self.assertRaises(ExperimentCancelled):
            run_experiment(df, config, plan, cancelled=lambda: True)

    def test_numeric_multiclass_target_is_supported(self):
        df = pd.DataFrame({
            "x": np.arange(90) % 7,
            "cat": pd.Series(["a", "b", None] * 30, dtype="string"),
            "target": pd.Series(np.arange(90) % 3, dtype="Int64"),
        })
        config = {
            "task": "classification", "target": "target", "features": ["x", "cat"],
            "split": "random", "folds": 3, "seed": 8, "model": "mlp",
            "imputer": "median", "encoding": "ordinal", "scaling": "robust",
            "average": "weighted", "params": {"epochs": 2, "hidden_layer_sizes": [5]},
        }
        result = run_experiment(df, config, make_split_plan(df, config))
        self.assertEqual(result["classes"], [0, 1, 2])
        self.assertEqual(result["summary"]["accuracy"]["n"], 3)

    def test_malformed_task_metric_and_class_weight_are_rejected(self):
        df = pd.DataFrame({"x": range(30), "target": np.linspace(1, 3, 30)})
        base = {
            "task": "regression", "target": "target", "features": ["x"],
            "split": "random", "folds": 3, "seed": 1, "model": "linear",
        }
        plan = make_split_plan(df, base)
        with self.assertRaisesRegex(ValueError, "primary_metric"):
            run_experiment(df, dict(base, primary_metric="roc_auc"), plan)
        with self.assertRaisesRegex(ValueError, "class_weight"):
            run_experiment(df, dict(base, class_weight="custom"), plan)


if __name__ == "__main__":
    unittest.main()
