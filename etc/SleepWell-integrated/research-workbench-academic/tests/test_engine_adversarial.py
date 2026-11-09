from __future__ import annotations

import unittest
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

from researchbench.engine import evaluate_final, make_split_plan, resolve_config, run_experiment
from researchbench.metrics import regression_metrics
from researchbench.metrics import classification_metrics
from researchbench.data import fingerprint
from researchbench.queueing import ExperimentQueue
from researchbench.storage import Project


class AdversarialStatisticalTests(unittest.TestCase):
    def test_resolve_config_uses_development_only_and_preserves_input(self):
        df = pd.DataFrame({
            "x": np.arange(60), "when": np.arange(60),
            "target": ["a", "b"] * 25 + ["final_only"] * 10,
        })
        config = {
            "task": "classification", "target": "target", "features": ["x"],
            "split": "time", "time": "when", "folds": 3, "model": "dummy",
            "positive_class": None,
        }
        plan = make_split_plan(df, config)
        resolved = resolve_config(df, config, plan)
        self.assertIsNone(config["positive_class"])
        self.assertEqual(resolved["positive_class"], "b")
        self.assertNotEqual(resolved["positive_class"], "final_only")

    def test_resolve_config_resolves_binned_binary_and_rejects_bad_constructor_keys(self):
        df = pd.DataFrame({"x": np.arange(50), "target": np.linspace(-2, 2, 50)})
        config = {
            "task": "classification", "target": "target", "features": ["x"],
            "split": "random", "folds": 3, "seed": 4, "model": "linear",
            "bin_edges": [0.0], "bin_rationale": "pre-specified threshold",
            "positive_class": None,
        }
        plan = make_split_plan(df, config)
        resolved = resolve_config(df, config, plan)
        self.assertEqual(resolved["positive_class"], "> 0")
        with self.assertRaises(TypeError):
            resolve_config(df, dict(config, params={"not_a_real_parameter": 1}), plan)

    def test_queue_persists_resolved_positive_without_immutable_config_failure(self):
        df = pd.DataFrame({"x": np.linspace(-2, 2, 60)})
        df["label"] = np.where(df.x > 0, "yes", "no")
        df["continuous"] = df.x
        digest = fingerprint(df)
        configs = [
            {
                "name": "default-positive", "task": "classification", "target": "label",
                "features": ["x"], "split": "random", "folds": 2, "seed": 8,
                "model": "dummy", "positive_class": None, "data_hash": digest,
            },
            {
                "name": "binned-positive", "task": "classification", "target": "continuous",
                "features": ["x"], "split": "random", "folds": 2, "seed": 8,
                "model": "dummy", "positive_class": None, "bin_edges": [0.0],
                "bin_rationale": "pre-specified", "data_hash": digest,
            },
        ]
        with tempfile.TemporaryDirectory() as folder:
            project = Project.create(Path(folder) / "resolved.rwb", df, "resolved")
            queue = ExperimentQueue(project)
            try:
                expected = ["yes", "> 0"]
                for config, expected_positive in zip(configs, expected):
                    plan = make_split_plan(df, config)
                    experiment_id = queue.submit(config, plan)
                    self.assertEqual(queue.records[experiment_id]["config"]["positive_class"], expected_positive)
                    deadline = time.monotonic() + 20
                    while queue.busy and time.monotonic() < deadline:
                        queue.poll()
                        time.sleep(0.02)
                    self.assertFalse(queue.busy)
                    record = queue.records[experiment_id]
                    self.assertEqual(record["status"], "completed", record)
                    self.assertEqual(record["config"]["positive_class"], expected_positive)
                    persisted = {item["id"]: item for item in project.experiments()}[experiment_id]
                    self.assertEqual(persisted["config"]["positive_class"], expected_positive)
            finally:
                queue.close()
                project.close()

    def test_default_binary_positive_class_is_recorded_not_implicit(self):
        df = pd.DataFrame({"x": np.linspace(-2, 2, 80)})
        df["target"] = np.where(df.x > 0, "healthy", "disease")
        config = {
            "task": "classification", "target": "target", "features": ["x"],
            "split": "random", "folds": 3, "seed": 3, "model": "linear",
        }
        result = run_experiment(df, config, make_split_plan(df, config))
        self.assertEqual(result["classes"], ["disease", "healthy"])
        self.assertEqual(result["config"]["positive_class"], "healthy")
        self.assertTrue(all(len(row["probabilities"]) == 2 for row in result["predictions"]))

    def test_all_missing_categorical_feature_is_imputed_and_encoded(self):
        df = pd.DataFrame({
            "numeric": np.arange(60, dtype=float), "empty_category": [None] * 60,
            "target": ["no", "yes"] * 30,
        })
        config = {
            "task": "classification", "target": "target",
            "features": ["numeric", "empty_category"], "split": "random",
            "folds": 3, "seed": 2, "model": "linear", "positive_class": "yes",
            "encoding": "onehot", "imputer": "median", "scaling": "standard",
        }
        result = run_experiment(df, config, make_split_plan(df, config))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"]["accuracy"]["n"], 3)

    def test_multiclass_fold_missing_a_class_marks_auc_and_ap_unavailable(self):
        df = pd.DataFrame({"x": np.arange(120), "target": [0] * 60 + [1] * 55 + [2] * 5})
        config = {
            "task": "classification", "target": "target", "features": ["x"],
            "split": "random", "stratify_target": None, "test_size": 0.1,
            "folds": 5, "seed": 1, "model": "dummy", "average": "macro",
        }
        result = run_experiment(df, config, make_split_plan(df, config))
        affected = [fold for fold in result["folds"] if fold["reasons"].get("average_precision")]
        self.assertTrue(affected)
        for fold in affected:
            self.assertIsNone(fold["metrics"]["roc_auc"])
            self.assertIsNone(fold["metrics"]["average_precision"])
            self.assertIn(fold["reasons"]["average_precision"], {"validation_missing_classes", "validation_has_one_class"})

    def test_constant_regression_target_has_undefined_r2(self):
        metrics, reasons = regression_metrics([5.0, 5.0, 5.0], [5.0, 5.0, 5.0])
        self.assertEqual(metrics["rmse"], 0.0)
        self.assertIsNone(metrics["r2"])
        self.assertEqual(reasons["r2"], "constant_target")

    def test_binary_zero_denominators_are_unavailable_not_forced_to_zero(self):
        metrics, reasons, _ = classification_metrics(
            ["no", "no"], [[0.9, 0.1], [0.8, 0.2]], ["no", "yes"],
            positive_class="yes", threshold=0.5,
        )
        self.assertIsNone(metrics["recall"])
        self.assertEqual(reasons["recall"], "validation_has_no_positive")
        self.assertIsNone(metrics["precision"])
        self.assertEqual(reasons["precision"], "no_predicted_positive")
        self.assertIsNone(metrics["f1"])
        self.assertEqual(reasons["f1"], "no_positive_in_truth_or_prediction")

    def test_multiclass_macro_does_not_zero_fill_undefined_class_metrics(self):
        classes = ["a", "b", "c"]
        y = ["a", "a", "b", "b"]
        probabilities = [
            [0.9, 0.1, 0.0], [0.8, 0.2, 0.0],
            [0.7, 0.3, 0.0], [0.6, 0.4, 0.0],
        ]
        metrics, reasons, _ = classification_metrics(
            y, probabilities, classes, average="macro"
        )
        self.assertIsNone(metrics["recall"])
        self.assertEqual(reasons["recall"], "validation_missing_classes")
        self.assertIsNone(metrics["precision"])
        self.assertEqual(reasons["precision"], "one_or_more_classes_never_predicted")
        self.assertIsNone(metrics["f1"])
        self.assertEqual(reasons["f1"], "class_absent_in_truth_and_prediction")
        self.assertIsNone(metrics["balanced_accuracy"])
        self.assertEqual(reasons["balanced_accuracy"], "validation_missing_classes")

    def test_multiclass_weighted_ignores_zero_support_but_not_unpredicted_supported_class(self):
        classes = ["a", "b", "c"]
        y = ["a", "a", "b", "b"]
        both_supported_predicted = [
            [0.9, 0.1, 0.0], [0.8, 0.2, 0.0],
            [0.2, 0.8, 0.0], [0.3, 0.7, 0.0],
        ]
        metrics, reasons, _ = classification_metrics(
            y, both_supported_predicted, classes, average="weighted"
        )
        self.assertEqual(metrics["recall"], 1.0)
        self.assertEqual(metrics["precision"], 1.0)
        self.assertEqual(metrics["f1"], 1.0)
        self.assertNotIn("precision", reasons)

        never_predict_b = [[0.9, 0.1, 0.0]] * 4
        metrics, reasons, _ = classification_metrics(
            y, never_predict_b, classes, average="weighted"
        )
        self.assertIsNone(metrics["precision"])
        self.assertEqual(reasons["precision"], "one_or_more_classes_never_predicted")
        self.assertAlmostEqual(metrics["recall"], 0.5)
        self.assertAlmostEqual(metrics["f1"], 1.0 / 3.0)

    def test_multiclass_weighted_specificity_is_unavailable_with_no_negatives(self):
        metrics, reasons, _ = classification_metrics(
            ["a", "a"], [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
            ["a", "b", "c"], average="weighted",
        )
        self.assertIsNone(metrics["specificity"])
        self.assertEqual(reasons["specificity"], "class_has_no_negative_examples")

    def test_final_only_class_is_rejected_instead_of_folded_into_negative_class(self):
        df = pd.DataFrame({
            "x": np.arange(60), "when": np.arange(60),
            "target": ["a", "b"] * 25 + ["c"] * 10,
        })
        config = {
            "task": "classification", "target": "target", "features": ["x"],
            "split": "time", "time": "when", "folds": 3, "model": "dummy",
        }
        plan = make_split_plan(df, config)
        self.assertNotIn("c", set(df.iloc[plan["train_rows"]].target))
        self.assertIn("c", set(df.iloc[plan["test_rows"]].target))
        with self.assertRaisesRegex(ValueError, "absent from development"):
            evaluate_final(df, config, plan)

    def test_dual_tasks_share_plan_but_report_distinct_effective_rows(self):
        n = 80
        df = pd.DataFrame({
            "x": np.arange(n),
            "class_target": pd.Series([None] * 20 + ["no", "yes"] * 30, dtype="object"),
            "reg_target": [float(v) if v < 60 else np.nan for v in range(n)],
        })
        shared = ["class_target", "reg_target"]
        classification = {
            "task": "classification", "target": "class_target", "features": ["x"],
            "shared_targets": shared, "stratify_target": None, "split": "random",
            "folds": 4, "seed": 17, "model": "dummy", "positive_class": "yes",
        }
        plan = make_split_plan(df, classification)
        regression = {
            "task": "regression", "target": "reg_target", "features": ["x"],
            "shared_targets": shared, "split": "random", "folds": 4,
            "seed": 17, "model": "dummy",
        }
        class_result = run_experiment(df, classification, plan)
        reg_result = run_experiment(df, regression, plan)
        self.assertEqual(class_result["split_id"], reg_result["split_id"])
        class_rows = {row["row_id"] for row in class_result["predictions"]}
        reg_rows = {row["row_id"] for row in reg_result["predictions"]}
        self.assertNotEqual(class_rows, reg_rows)
        self.assertGreater(class_result["row_scope"]["excluded_development_target_missing"], 0)
        self.assertGreater(reg_result["row_scope"]["excluded_development_target_missing"], 0)

    def test_mlp_regression_history_uses_original_target_units_for_both_curves(self):
        rng = np.random.default_rng(21)
        df = pd.DataFrame({"x": np.linspace(-2, 2, 90)})
        df["target"] = 10_000.0 + 800.0 * df.x + rng.normal(0, 25, len(df))
        config = {
            "task": "regression", "target": "target", "features": ["x"],
            "split": "random", "folds": 3, "seed": 9, "model": "mlp",
            "target_transform": "standard", "scaling": "standard",
            "params": {"epochs": 3, "hidden_layer_sizes": [5]},
        }
        result = run_experiment(df, config, make_split_plan(df, config))
        self.assertEqual(len(result["history"]), 9)
        for epoch in result["history"]:
            self.assertEqual(epoch["loss_metric"], "mse_original_unit")
            self.assertIsNotNone(epoch["valid_loss"])
            self.assertGreater(epoch["train_loss"], 1_000.0)
            self.assertGreater(epoch["valid_loss"], 1_000.0)
            self.assertLess(epoch["optimizer_loss"], epoch["train_loss"] / 100.0)

    def test_final_mlp_never_reports_epoch_test_loss(self):
        df = pd.DataFrame({"x": np.linspace(-1, 1, 70)})
        df["target"] = 50.0 + 10.0 * df.x
        config = {
            "task": "regression", "target": "target", "features": ["x"],
            "split": "random", "folds": 2, "seed": 7, "model": "mlp",
            "target_transform": "standard", "params": {"epochs": 4, "hidden_layer_sizes": [4]},
        }
        plan = make_split_plan(df, config)
        events = []
        result = evaluate_final(df, config, plan, progress=events.append)
        self.assertEqual(len(result["history"]), 4)
        self.assertTrue(all(epoch["valid_loss"] is None for epoch in result["history"]))
        epoch_events = [event for event in events if event.get("epoch")]
        self.assertEqual(len(epoch_events), 4)
        self.assertTrue(all(event["valid_loss"] is None for event in epoch_events))
        self.assertEqual({row["row_id"] for row in result["predictions"]}, set(plan["test_rows"]))


if __name__ == "__main__":
    unittest.main()
