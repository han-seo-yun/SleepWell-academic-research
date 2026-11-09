from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")

from researchbench.data import data_profile, fingerprint, load_data
from researchbench.plotting import comparison_table, export_figure, export_tables, make_figure
from researchbench.storage import Project


def classification_result(experiment_id: str, mean_shift: float = 0.0) -> dict:
    return {
        "id": experiment_id, "name": f"Experiment {experiment_id}", "task": "classification",
        "phase": "validation", "split_id": "same-split", "data_hash": "hash",
        "status": "completed", "duration": 1.2,
        "config": {"target": "outcome", "positive_class": "yes"}, "classes": ["no", "yes"],
        "folds": [
            {"fold": 1, "metrics": {"accuracy": .70 + mean_shift, "roc_auc": .75 + mean_shift}},
            {"fold": 2, "metrics": {"accuracy": .80 + mean_shift, "roc_auc": .85 + mean_shift}},
        ],
        "predictions": [
            {"row_id": 0, "fold": 1, "y_true": "no", "y_pred": "no", "probabilities": [.8, .2]},
            {"row_id": 1, "fold": 1, "y_true": "yes", "y_pred": "yes", "probabilities": [.2, .8]},
            {"row_id": 2, "fold": 2, "y_true": "no", "y_pred": "no", "probabilities": [.7, .3]},
            {"row_id": 3, "fold": 2, "y_true": "yes", "y_pred": "yes", "probabilities": [.1, .9]},
        ],
        "history": [],
    }


class DataTests(unittest.TestCase):
    def test_load_profile_and_fingerprint_detect_real_quality_issues(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data.csv"
            path.write_text("id,x,mixed,constant\n1,1,2,A\n2,,bad,A\n2,,bad,A\n", encoding="utf-8")
            frame = load_data(path)
            profile = data_profile(frame)
            codes = {(warning["code"], warning.get("column")) for warning in profile["warnings"]}
            self.assertEqual(frame.index.tolist(), [0, 1, 2])
            self.assertEqual(profile["duplicates"], 1)
            self.assertIn(("constant_feature", "constant"), codes)
            self.assertIn(("high_missingness", "x"), codes)
            self.assertIn(("mixed_numeric_text", "mixed"), codes)
            first = fingerprint(frame)
            changed = frame.copy(); changed.loc[0, "x"] = 99
            self.assertNotEqual(first, fingerprint(changed))
            self.assertEqual(first, fingerprint(frame.copy()))


class StorageTests(unittest.TestCase):
    def test_nonfinite_values_are_profiled_and_rejected_before_directory_creation(self):
        frame = pd.DataFrame({"measurement": [1.0, np.inf, -np.inf]})
        warnings = data_profile(frame)["warnings"]
        self.assertIn({"code": "nonfinite_values", "column": "measurement", "count": 2}, warnings)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "must-not-exist"
            with self.assertRaisesRegex(ValueError, "nonfinite_values:measurement"):
                Project.create(root, frame, "Invalid")
            self.assertFalse(root.exists())

    def test_metadata_save_does_not_rewrite_snapshot_or_relabel_data_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "study"
            project = Project.create(root, pd.DataFrame({"x": [1.0, 2.0]}), "Study")
            snapshot = root / "data.json"
            before_time = snapshot.stat().st_mtime_ns
            before_hash = project.state["data_hash"]
            project.state["ui"]["language"] = "en"
            time.sleep(.01)
            project.save()
            self.assertEqual(snapshot.stat().st_mtime_ns, before_time)
            self.assertEqual(project.state["data_hash"], before_hash)
            project.df.loc[0, "x"] = 9.0
            with self.assertRaisesRegex(ValueError, "new_data_version_required"):
                project.save()
            self.assertEqual(project.state["data_hash"], before_hash)

    def test_projects_created_from_csv_and_excel_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = pd.DataFrame({
                "visit_date": pd.to_datetime(["2025-01-01", "2025-01-02"]),
                "measurement": [0.123456789012345, 9.876543210987654],
                "site": ["서울", "부산"],
            })
            csv_path, excel_path = directory / "sample.csv", directory / "sample.xlsx"
            source.assign(visit_date=source["visit_date"].dt.strftime("%Y-%m-%d")).to_csv(csv_path, index=False)
            source.to_excel(excel_path, index=False)
            for input_path in (csv_path, excel_path):
                loaded = load_data(input_path)
                root = directory / f"{input_path.stem}_{input_path.suffix[1:]}_project"
                Project.create(root, loaded, "Round trip", str(input_path)).close()
                reopened = Project.open(root)
                self.assertEqual(reopened.state["data_hash"], fingerprint(reopened.df))
                self.assertFalse(reopened.state["source_changed"])

    def test_save_reopen_restore_and_hash_integrity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "study.rwb"
            frame = pd.DataFrame({"age": [20, 30], "group": ["a", "b"], "when": pd.to_datetime(["2024-01-01", "2024-01-02"])})
            project = Project.create(root, frame, "Study")
            project.state["roles"] = {"age": "feature", "group": "group"}
            project.save_experiment({"id": "run-1", "status": "completed", "config": {"model": "linear"}, "folds": []})
            project.close()
            reopened = Project.open(root)
            self.assertEqual(reopened.state["roles"]["group"], "group")
            self.assertEqual(reopened.experiments()[0]["id"], "run-1")
            self.assertEqual(fingerprint(reopened.df), reopened.state["data_hash"])
            with self.assertRaisesRegex(ValueError, "immutable"):
                reopened.save_experiment({"id": "run-1", "status": "completed", "config": {"model": "forest"}, "folds": []})

    def test_open_marks_active_jobs_interrupted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "study"
            project = Project.create(root, pd.DataFrame({"x": [1, 2]}), "Study")
            project.save_experiment({"id": "run-2", "status": "running", "config": {"model": "linear"}})
            project.close()
            record = Project.open(root).experiments()[0]
            self.assertEqual(record["status"], "interrupted")
            self.assertIn("interrupted_reason", record)

    def test_snapshot_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "study"
            Project.create(root, pd.DataFrame({"x": [1, 2]}), "Study").close()
            payload = json.loads((root / "data.json").read_text(encoding="utf-8"))
            payload["data"][0]["x"] = 100
            (root / "data.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash_mismatch"):
                Project.open(root)


class PlottingTests(unittest.TestCase):
    def test_numeric_class_target_uses_categories_and_regression_uses_histogram(self):
        frame = pd.DataFrame({"feature": [1, 2, 3, 4, 5, 6], "coded_target": [0, 0, 1, 1, 2, 2]})
        classification = make_figure("target", df=frame, target="coded_target", lang="en", options={"task": "classification"})
        self.assertEqual([tick.get_text() for tick in classification.axes[0].get_xticklabels()], ["0", "1", "2"])
        grouped = make_figure("box", df=frame, feature="feature", target="coded_target", lang="en", options={"task": "classification"})
        self.assertEqual([tick.get_text() for tick in grouped.axes[0].get_xticklabels()], ["0", "1", "2"])
        regression = make_figure("target", df=frame, target="coded_target", lang="en", options={"task": "regression"})
        self.assertNotEqual([tick.get_text() for tick in regression.axes[0].get_xticklabels()], ["0", "1", "2"])

    def test_language_custom_title_and_publication_exports(self):
        frame = pd.DataFrame({"x": [1.0, 2.0, 3.0], "y": [2.0, 1.0, 4.0]})
        english = make_figure("correlation", df=frame, lang="en")
        korean = make_figure("missing", df=frame, lang="ko")
        custom = make_figure("histogram", df=frame, feature="x", lang="ko", options={"title": "My unchanged title"})
        self.assertIn("correlation", english.axes[0].get_title().lower())
        self.assertIn("결측치", korean.axes[0].get_title())
        self.assertEqual(custom.axes[0].get_title(), "My unchanged title")
        with tempfile.TemporaryDirectory() as temporary:
            for extension in ("png", "pdf", "svg"):
                output = Path(temporary) / f"figure.{extension}"
                export_figure(korean, output)
                self.assertGreater(output.stat().st_size, 500)

    def test_comparison_uses_percentage_points_and_refuses_cross_task(self):
        baseline = classification_result("base")
        better = classification_result("better", .05)
        regression = {**classification_result("regression"), "task": "regression"}
        table = comparison_table([baseline, better, regression], "base", lang="en")
        accuracy = table[(table["experiment_id"] == "better") & (table["metric"] == "accuracy")].iloc[0]
        self.assertAlmostEqual(accuracy["delta_raw"], .05)
        self.assertAlmostEqual(accuracy["delta_percentage_points"], 5.0)
        cross_task = table[table["experiment_id"] == "regression"].iloc[0]
        self.assertFalse(bool(cross_task["comparable"]))
        self.assertEqual(cross_task["comparison_reason"], "different_task")

    def test_r2_is_not_percentage_points_and_incompatible_split_is_not_plotted(self):
        baseline = classification_result("base")
        regression_base = {**baseline, "id": "reg-base", "task": "regression", "config": {"target": "score"}}
        regression_base["folds"] = [{"fold": 1, "metrics": {"r2": .4}}]
        regression_other = {**regression_base, "id": "reg-other", "name": "Other", "split_id": "other-split"}
        table = comparison_table([regression_base], "reg-base", lang="en")
        self.assertTrue(pd.isna(table.iloc[0]["delta_percentage_points"]))
        figure = make_figure("comparison", results=[regression_base, regression_other], metric="r2", baseline_id="reg-base", lang="en")
        labels = [tick.get_text() for tick in figure.axes[0].get_xticklabels()]
        self.assertEqual(labels, [regression_base["name"]])

    def test_delta_plot_uses_paired_fold_differences(self):
        baseline = classification_result("base")
        variant = classification_result("variant")
        baseline["folds"][0]["metrics"]["accuracy"] = .1
        baseline["folds"][1]["metrics"]["accuracy"] = .8
        variant["folds"][0]["metrics"]["accuracy"] = .2
        variant["folds"][1]["metrics"]["accuracy"] = .9
        for result in (baseline, variant):
            result["folds"][0]["valid_rows"] = [0, 1]
            result["folds"][1]["valid_rows"] = [2, 3]
        figure = make_figure("delta", results=[baseline, variant], metric="accuracy", baseline_id="base", lang="en")
        point_sets = [collection.get_offsets() for collection in figure.axes[0].collections if hasattr(collection, "get_offsets") and len(collection.get_offsets()) == 2]
        self.assertTrue(any(np.allclose(np.asarray(points)[:, 1], [.1, .1]) for points in point_sets))
        self.assertIn("paired fold deltas", figure.axes[0].texts[-1].get_text())

    def test_threshold_change_remains_comparable_and_is_recorded_in_config(self):
        baseline = classification_result("base")
        changed = classification_result("changed")
        baseline["config"]["threshold"] = .5
        changed["config"]["threshold"] = .7
        table = comparison_table([baseline, changed], "base", lang="en")
        accuracy = table[(table["experiment_id"] == "changed") & (table["metric"] == "accuracy")].iloc[0]
        auc = table[(table["experiment_id"] == "changed") & (table["metric"] == "roc_auc")].iloc[0]
        self.assertTrue(bool(accuracy["comparable"]))
        self.assertTrue(bool(auc["comparable"]))

    def test_result_figures_and_tables_use_recorded_predictions(self):
        baseline = classification_result("base")
        better = classification_result("better", .02)
        for kind in ("comparison", "delta", "roc", "pr", "confusion"):
            figure = make_figure(kind, results=[baseline, better], baseline_id="base", metric="accuracy", lang="en")
            self.assertTrue(figure.axes)
            self.assertFalse(any("No data" in text.get_text() for text in figure.axes[0].texts))
        with tempfile.TemporaryDirectory() as temporary:
            csv_path = Path(temporary) / "results.csv"
            xlsx_path = Path(temporary) / "results.xlsx"
            export_tables([baseline, better], csv_path, "base", "en")
            export_tables([baseline, better], xlsx_path, "base", "ko")
            self.assertIn("Percentage-point change", pd.read_csv(csv_path).columns)
            self.assertTrue((Path(temporary) / "results_folds.csv").is_file())
            self.assertTrue((Path(temporary) / "results_predictions.csv").is_file())
            self.assertTrue((Path(temporary) / "results_history.csv").is_file())
            self.assertIn("Class probabilities", pd.read_csv(Path(temporary) / "results_predictions.csv").columns)
            self.assertIn("Loss metric key", pd.read_csv(Path(temporary) / "results_history.csv").columns)
            with pd.ExcelFile(xlsx_path) as workbook:
                self.assertEqual(set(workbook.sheet_names), {"실험 비교", "fold 지표", "예측값", "학습 이력"})
                self.assertIn("검증 손실", pd.read_excel(workbook, sheet_name="학습 이력").columns)

    def test_final_learning_curve_omits_hidden_validation_loss(self):
        result = classification_result("mlp")
        result["phase"] = "final_test"
        result["history"] = [
            {"fold": 1, "epoch": 1, "train_loss": .8, "valid_loss": None, "loss_metric": "cross_entropy"},
            {"fold": 1, "epoch": 2, "train_loss": .6, "valid_loss": None, "loss_metric": "cross_entropy"},
        ]
        figure = make_figure("learning", results=[result], lang="en")
        labels = [line.get_label() for line in figure.axes[0].lines]
        self.assertEqual(labels, ["Training"])
        self.assertEqual(figure.axes[0].get_ylabel(), "Cross-entropy loss")

    def test_regression_axes_include_unit_and_evaluation_scope(self):
        result = {
            "id": "reg", "name": "Regression", "task": "regression", "phase": "validation",
            "split_id": "split", "data_hash": "hash", "config": {"target": "score", "target_unit": "mg/dL"},
            "folds": [{"fold": 1, "metrics": {"rmse": 1.0}}, {"fold": 2, "metrics": {"rmse": 2.0}}],
            "predictions": [
                {"row_id": 1, "fold": 1, "y_true": 2.0, "y_pred": 2.5, "probabilities": None},
                {"row_id": 2, "fold": 2, "y_true": 3.0, "y_pred": 2.7, "probabilities": None},
            ], "classes": [], "history": [],
        }
        figure = make_figure("predicted", results=[result], lang="en")
        self.assertIn("mg/dL", figure.axes[0].get_xlabel())
        self.assertIn("mg/dL", figure.axes[0].get_ylabel())
        self.assertTrue(any("n=2" in text.get_text() and "folds=2" in text.get_text() for text in figure.axes[0].texts))


if __name__ == "__main__":
    unittest.main()
