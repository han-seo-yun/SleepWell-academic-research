from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from researchbench.advanced_evaluation import (
    _manifest_hash,
    evaluate_external,
    freeze_general,
    paired_compare,
)
from researchbench.engine import evaluate_final, make_split_plan
from researchbench.storage import Project


class FrozenExternalEvaluationTests(unittest.TestCase):
    def _classification_project(self, root: Path) -> tuple[Path, dict, pd.DataFrame]:
        rng = np.random.default_rng(12)
        size = 72
        frame = pd.DataFrame({
            "x": rng.normal(size=size),
            "category": np.where(np.arange(size) % 3 == 0, "a", "b"),
            "group": np.where(np.arange(size) % 4 == 0, "g1", "g2"),
        })
        frame["target"] = ((frame["x"] + (frame["category"] == "a") * .4) > 0).astype(int)
        path = root / "project"
        project = Project.create(path, frame, "frozen-test")
        config = {
            "id": "chosen-final", "name": "chosen final", "task": "classification",
            "target": "target", "features": ["x", "category"], "model": "linear",
            "split": "random", "folds": 3, "test_size": .2, "seed": 17,
            "imputer": "median", "encoding": "onehot", "scaling": "standard",
            "positive_class": 1, "threshold": .5, "average": "macro",
            "data_hash": project.state["data_hash"],
        }
        plan = make_split_plan(frame, config)
        project.state["splits"] = {plan["id"]: plan}
        project.save()
        result = evaluate_final(frame, config, plan)
        project.save_experiment(result)
        project.close()
        return path, plan, frame

    def test_freeze_uses_development_rows_only_and_external_is_reloadable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path, plan, frame = self._classification_project(root)
            project_before = (project_path / "project.json").read_bytes()
            manifest = freeze_general(project_path, "chosen-final")
            self.assertEqual(set(manifest["training_row_ids"]), set(plan["train_rows"]))
            self.assertTrue(set(manifest["training_row_ids"]).isdisjoint(plan["test_rows"]))
            self.assertEqual((project_path / "project.json").read_bytes(), project_before)
            external = root / "external.csv"
            frame.assign(group=np.where(np.arange(len(frame)) % 5 == 0, "small", "large")).to_csv(
                external, index=False
            )
            artifact = project_path / "research" / "frozen" / manifest["artifact_id"]
            result = evaluate_external(artifact, external, "independent-example", subgroup="group")
            self.assertEqual(result["n_evaluated"], len(frame))
            self.assertEqual(result["input_designation"], "unknown")
            self.assertEqual(result["evaluation_metadata"]["scope"],
                             "external_frozen_no_selection")
            self.assertFalse(result["evaluation_metadata"]["refit"])
            self.assertEqual(result["config"], result["provenance"]["source_run"]["config"])
            self.assertEqual(result["data_hash"], manifest["project_data_hash"])
            self.assertEqual(result["split_id"], manifest["split_id"])
            self.assertEqual(result["phase"], "external_evaluation")
            self.assertEqual(result["provenance"]["source_run"]["id"], "chosen-final")
            self.assertEqual(result["provenance"]["source_run"]["project_data_hash"],
                             manifest["project_data_hash"])
            self.assertEqual(result["provenance"]["frozen_artifact"]["model_sha256"],
                             manifest["model_sha256"])
            self.assertEqual(result["provenance"]["external_input"]["designation_source"],
                             "default_unknown")
            self.assertIsNotNone(result["metrics"]["roc_auc"])
            self.assertTrue(result["calibration"]["available"])
            self.assertEqual(sum(row["n"] for row in result["calibration"]["bins"]), len(frame))
            self.assertEqual(result["metric_units"]["brier_score"], "dimensionless")
            self.assertFalse(result["subgroup_uncertainty"]["available"])
            self.assertEqual(result["subgroup_uncertainty"]["reason"],
                             "independent_resampling_unit_not_provided")
            self.assertEqual(evaluate_external(artifact, external, "independent-example", subgroup="group"), result)
            synthetic = evaluate_external(
                artifact, external, "independent-example", subgroup="group",
                input_designation="declared_synthetic",
            )
            self.assertNotEqual(synthetic["id"], result["id"])
            self.assertEqual(synthetic["input_designation"], "declared_synthetic")
            self.assertEqual(synthetic["provenance"]["external_input"]["designation_source"],
                             "user_explicit")

    def test_external_input_designation_is_closed_and_selection_scope_is_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path, _, frame = self._classification_project(root)
            manifest = freeze_general(project_path, "chosen-final")
            artifact = project_path / "research" / "frozen" / manifest["artifact_id"]
            external = root / "external.csv"
            frame.to_csv(external, index=False)
            with self.assertRaisesRegex(ValueError, "input_designation"):
                evaluate_external(artifact, external, "invalid", input_designation="real")
            selected = evaluate_external(
                artifact, external, "selected-after-viewing",
                selection_after_external=True, input_designation="declared_observed",
            )
            self.assertEqual(selected["evaluation_metadata"]["scope"],
                             "external_after_model_selection")
            self.assertTrue(selected["evaluation_metadata"]["selection_after_external"])

    def test_schema_and_artifact_hash_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path, _, frame = self._classification_project(root)
            manifest = freeze_general(project_path, "chosen-final")
            artifact = project_path / "research" / "frozen" / manifest["artifact_id"]
            missing = root / "missing.csv"
            frame.drop(columns="category").to_csv(missing, index=False)
            with self.assertRaisesRegex(ValueError, "missing required columns"):
                evaluate_external(artifact, missing, "schema-failure")
            model = artifact / "model.skops"
            model.write_bytes(model.read_bytes() + b"tamper")
            valid = root / "valid.csv"
            frame.to_csv(valid, index=False)
            with self.assertRaisesRegex(ValueError, "model hash mismatch"):
                evaluate_external(artifact, valid, "hash-failure")

    def test_recomputed_manifest_cannot_redirect_external_result_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path, _, frame = self._classification_project(root)
            manifest = freeze_general(project_path, "chosen-final")
            artifact = project_path / "research" / "frozen" / manifest["artifact_id"]
            redirect = root / "attacker-selected-existing-directory"
            redirect.mkdir()
            manifest_path = artifact / "manifest.json"
            altered = json.loads(manifest_path.read_text(encoding="utf-8"))
            altered["project_path"] = str(redirect)
            altered["manifest_sha256"] = _manifest_hash(altered)
            manifest_path.write_text(json.dumps(altered, ensure_ascii=False), encoding="utf-8")
            external = root / "external.csv"
            frame.to_csv(external, index=False)
            with self.assertRaisesRegex(ValueError, "redirects to another existing location"):
                evaluate_external(artifact, external, "must-not-write")
            self.assertFalse((redirect / "research" / "external").exists())

    def test_whole_project_tree_can_move_when_recorded_old_path_is_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path, _, frame = self._classification_project(root)
            manifest = freeze_general(project_path, "chosen-final")
            moved = root / "moved-project"
            shutil.move(str(project_path), moved)
            self.assertFalse(project_path.exists())
            external = root / "external.csv"
            frame.to_csv(external, index=False)
            result = evaluate_external(
                moved / "research" / "frozen" / manifest["artifact_id"],
                external, "portable-project",
            )
            self.assertTrue((moved / "research" / "external" / f"{result['id']}.json").is_file())

    def test_regression_target_is_inverse_transformed_in_original_unit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            x = np.linspace(-3, 3, 60)
            frame = pd.DataFrame({"x": x, "target": 20 + 4 * x})
            path = root / "regression"
            project = Project.create(path, frame, "regression")
            config = {"id": "reg-final", "name": "regression", "task": "regression",
                      "target": "target", "features": ["x"], "model": "linear",
                      "params": {"alpha": 1e-12},
                      "split": "random", "folds": 3, "test_size": .2, "seed": 4,
                      "imputer": "median", "encoding": "onehot", "scaling": "standard",
                      "target_transform": "standard", "target_unit": "minutes",
                      "data_hash": project.state["data_hash"]}
            plan = make_split_plan(frame, config)
            project.state["splits"] = {plan["id"]: plan}
            project.save()
            project.save_experiment(evaluate_final(frame, config, plan))
            project.close()
            manifest = freeze_general(path, "reg-final")
            external = root / "reg.csv"
            frame.to_csv(external, index=False)
            result = evaluate_external(path / "research" / "frozen" / manifest["artifact_id"],
                                       external, "regression-external")
            self.assertLess(result["metrics"]["mae"], 1e-8)
            self.assertEqual(result["target_unit"], "minutes")
            self.assertEqual(result["metric_units"]["mae"], "minutes")
            self.assertEqual(result["metric_units"]["mse"], "(minutes)²")
            self.assertEqual(result["metric_units"]["rmse"], "minutes")
            self.assertEqual(result["residual_summary"]["definition"],
                             "observed_minus_predicted_original_unit")
            invalid = root / "reg_invalid.csv"
            invalid_frame = frame.copy()
            invalid_frame["target"] = invalid_frame["target"].astype(object)
            invalid_frame.loc[0, "target"] = "not-a-number"
            invalid_frame.to_csv(invalid, index=False)
            with self.assertRaisesRegex(ValueError, "numerical target"):
                evaluate_external(path / "research" / "frozen" / manifest["artifact_id"],
                                  invalid, "regression-invalid")


class PairedComparisonTests(unittest.TestCase):
    def _result(self, offset: float = 0.0) -> dict:
        return {"predictions": [
            {"row_id": index, "y_true": float(index), "y_pred": float(index) + offset}
            for index in range(8)
        ]}

    def test_cluster_bootstrap_aligns_ids_and_reports_denominators(self):
        participants = {index: f"p{index // 2}" for index in range(8)}
        result = paired_compare(self._result(.5), self._result(.2), metric="mae",
                                participant_ids=participants, bootstrap_repeats=200, seed=8)
        self.assertEqual(result["n_paired_rows"], 8)
        self.assertEqual(result["n_independent_units"], 4)
        self.assertEqual(result["method"], "paired_participant_cluster_bootstrap")
        self.assertAlmostEqual(result["improvement"], .3)
        unmatched = self._result(.2)
        unmatched["predictions"].pop()
        with self.assertRaisesRegex(ValueError, "identical evaluation unit IDs"):
            paired_compare(self._result(.5), unmatched, metric="mae",
                           participant_ids=participants, bootstrap_repeats=100)

    def test_ordered_series_requires_explicit_block_length(self):
        with self.assertRaisesRegex(ValueError, "explicit block_length"):
            paired_compare(self._result(.5), self._result(.2), metric="rmse",
                           ordered=True, bootstrap_repeats=100)
        result = paired_compare(self._result(.5), self._result(.2), metric="rmse",
                                ordered=True, block_length=2, bootstrap_repeats=100, seed=2)
        self.assertEqual(result["method"], "paired_moving_block_bootstrap")
        self.assertEqual(result["block_length"], 2)

    def test_aggregate_only_reports_do_not_receive_fabricated_intervals(self):
        with self.assertRaisesRegex(ValueError, "requires raw predictions"):
            paired_compare({"metrics": {"mae": 1}}, {"metrics": {"mae": 2}},
                           metric="mae", participant_ids={}, bootstrap_repeats=100)

    def test_single_participant_reports_point_estimate_without_interval(self):
        result = paired_compare(
            self._result(.5), self._result(.2), metric="mae",
            participant_ids={index: "only-participant" for index in range(8)},
            bootstrap_repeats=100,
        )
        self.assertEqual(result["n_independent_units"], 1)
        self.assertEqual(result["method"], "paired_point_estimate_no_interval")
        self.assertIsNone(result["delta_ci_95_percentile"])
        self.assertFalse(result["uncertainty_available"])
        self.assertEqual(result["uncertainty_reason"],
                         "fewer_than_two_independent_participants")

    def test_missing_nonfinite_and_incompatible_paired_values_are_rejected(self):
        missing_mapping = {index: f"p{index}" for index in range(7)}
        with self.assertRaisesRegex(ValueError, "mapping is missing"):
            paired_compare(self._result(.5), self._result(.2), metric="mae",
                           participant_ids=missing_mapping, bootstrap_repeats=100)
        invalid = self._result(.2)
        invalid["predictions"][0]["y_pred"] = float("nan")
        with self.assertRaisesRegex(ValueError, "missing labels|must be finite"):
            paired_compare(self._result(.5), invalid, metric="mae",
                           participant_ids={index: f"p{index}" for index in range(8)},
                           bootstrap_repeats=100)
        missing_label = self._result(.2)
        missing_label["predictions"][0]["y_true"] = None
        with self.assertRaisesRegex(ValueError, "missing labels"):
            paired_compare(self._result(.5), missing_label, metric="accuracy",
                           participant_ids={index: f"p{index}" for index in range(8)},
                           bootstrap_repeats=100)
        left = {**self._result(.5), "task": "regression", "target_unit": "minutes"}
        right = {**self._result(.2), "task": "regression", "target_unit": "hours"}
        with self.assertRaisesRegex(ValueError, "same target unit"):
            paired_compare(left, right, metric="mse",
                           participant_ids={index: f"p{index}" for index in range(8)},
                           bootstrap_repeats=100)


if __name__ == "__main__":
    unittest.main()
