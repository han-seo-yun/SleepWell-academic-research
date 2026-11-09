from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from researchbench.data import fingerprint, load_data
from researchbench.engine import make_split_plan, run_experiment
from researchbench.plotting import export_figure, export_tables, make_figure
from researchbench.storage import Project


ROOT = Path(__file__).resolve().parents[1]


def _config(task: str, target: str, features: list[str], digest: str, experiment_id: str) -> dict:
    is_classification = task == "classification"
    return {
        "id": experiment_id,
        "name": experiment_id,
        "task": task,
        "target": target,
        "features": features,
        "model": "linear",
        "params": {"max_iter": 400},
        "imputer": "median",
        "encoding": "onehot",
        "scaling": "standard",
        "positive_class": "yes" if is_classification else None,
        "threshold": 0.5,
        "average": "macro",
        "class_weight": "balanced" if is_classification else None,
        "target_unit": "score" if not is_classification else None,
        "target_transform": "none",
        "device": "cpu",
        "primary_metric": "roc_auc" if is_classification else "rmse",
        "data_hash": digest,
    }


class EndToEndTests(unittest.TestCase):
    def _run_task(self, task: str, filename: str, target: str, metric: str) -> None:
        df = load_data(ROOT / "samples" / filename)
        digest = fingerprint(df)
        features = ["age", "biomarker", "treatment", "site"]
        baseline = _config(task, target, features, digest, f"{task}-base")
        baseline["seed"] = 73
        plan = make_split_plan(
            df,
            {**baseline, "split": "random", "test_size": 0.2, "folds": 3, "seed": 73},
        )
        changed = _config(task, target, ["age", "treatment", "site"], digest, f"{task}-drop")
        changed["seed"] = 73
        base_result = run_experiment(df, baseline, plan)
        changed_result = run_experiment(df, changed, plan)

        self.assertEqual(base_result["status"], "completed")
        self.assertEqual(changed_result["status"], "completed")
        self.assertEqual(base_result["split_id"], changed_result["split_id"])
        self.assertEqual(base_result["split_id"], plan["id"])
        self.assertEqual(
            [fold["valid_rows"] for fold in base_result["folds"]],
            [fold["valid_rows"] for fold in changed_result["folds"]],
        )
        self.assertGreater(base_result["summary"][metric]["n"], 0)
        test_rows = set(plan["test_rows"])
        used_validation_rows = {row["row_id"] for row in base_result["predictions"]}
        self.assertTrue(test_rows.isdisjoint(used_validation_rows), "final test rows leaked into validation")

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project_path = root / f"{task}.rwb"
            project = Project.create(project_path, df, task, str(ROOT / "samples" / filename))
            project.state["baselines"] = {task: baseline["id"]}
            project.state["splits"] = {plan["id"]: plan}
            project.save_experiment(base_result)
            project.save_experiment(changed_result)
            project.save()
            project.close()

            reopened = Project.open(project_path)
            restored = reopened.experiments()
            self.assertEqual({r["id"] for r in restored}, {baseline["id"], changed["id"]})
            self.assertEqual(reopened.state["data_hash"], digest)
            reopened.close()

            figure = make_figure(
                "comparison", results=restored, metric=metric, lang="ko", baseline_id=baseline["id"]
            )
            for suffix in ("png", "pdf", "svg"):
                path = root / f"figure.{suffix}"
                export_figure(figure, path, dpi=180)
                self.assertGreater(path.stat().st_size, 500)
            for suffix in ("csv", "xlsx"):
                path = root / f"table.{suffix}"
                export_tables(restored, path, baseline_id=baseline["id"], lang="ko")
                self.assertGreater(path.stat().st_size, 50)

    def test_classification_baseline_feature_removal_persist_export(self) -> None:
        self._run_task("classification", "classification_mixed.csv", "response", "roc_auc")

    def test_regression_baseline_feature_removal_persist_export(self) -> None:
        self._run_task("regression", "regression_mixed.xlsx", "outcome_score", "rmse")


if __name__ == "__main__":
    unittest.main()
