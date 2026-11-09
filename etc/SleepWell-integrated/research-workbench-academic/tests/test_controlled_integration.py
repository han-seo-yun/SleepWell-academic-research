from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from matplotlib.container import ErrorbarContainer

from researchbench import academic_jobs
from researchbench.academic_plotting import export_academic_tables, make_academic_figure, model_label
from researchbench.result_adapter import adapt_run, compare
from researchbench.reporting import ReportService


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compare_cmi_mesa", ROOT / "scripts" / "compare_cmi_mesa.py"
)
cli = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cli)


def controlled_envelope() -> dict:
    summary = []
    for cohort, prefix, offset, people, nights in (
        ("CMI", "cmi", 0.0, 44, 673),
        ("MESA", "mesa", 20.0, 315, 2175),
        ("MACRO", "macro", 10.0, 359, 2848),
    ):
        for model, delta in (("separate_data", 0.0), ("pooled_data", .7)):
            joint = 48.0 + offset + delta
            summary.append({
                "model_id": f"{prefix}.{model}", "model": model, "cohort": cohort,
                "split": "test", "onset_mae_min": joint + 1,
                "wake_mae_min": joint - 1, "joint_mae_min": joint,
                "ci95_low": joint - 2, "ci95_high": joint + 2,
                "n_participants": people, "n_nights": nights,
            })
    paired = [{
        "reference": "separate_data", "model": "pooled_data", "cohort": cohort,
        "improvement_min": -.7, "ci95_low": -1.2, "ci95_high": .2,
        "n_participants": people,
    } for cohort, people in (("CMI", 44), ("MESA", 315), ("MACRO", 359))]
    return {
        "mode": "controlled_comparison",
        "raw": {"status": "retrospective_reused_holdout",
                "verification": {"database_unchanged": True},
                "primary_pair": ["separate_data", "pooled_data"],
                "paired_comparisons": paired},
        "summary": summary,
        "provenance": {"input_synthetic": False},
        "warnings": [],
    }


class ControlledComparisonIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "cmi_mesa.sqlite3"
        self.database.write_bytes(b"controlled fixture database")
        self.output = self.root / "result"

    def tearDown(self):
        self.temporary.cleanup()

    def test_dispatch_uses_controlled_engine_and_real_database_provenance(self):
        envelope = controlled_envelope()
        compare = mock.Mock(return_value=envelope)
        module = types.ModuleType("researchbench.controlled_comparison")
        module.run_controlled_comparison = compare
        config = cli.make_config(
            self.database, self.output, seed=20261104,
            bootstrap_replicates=100, cv_repeats=2, cv_folds=3, max_seconds=30,
        )
        with mock.patch.dict(sys.modules, {"researchbench.controlled_comparison": module}):
            result = academic_jobs.execute(config, self.root)
        supplied = compare.call_args.args[0]
        self.assertEqual(supplied["database_path"], str(self.database.resolve()))
        self.assertEqual(supplied["output_dir"], str(self.output.resolve()))
        self.assertEqual(supplied["cv_repeats"], 2)
        self.assertEqual(supplied["cv_folds"], 3)
        self.assertFalse(supplied["input_synthetic"])
        self.assertEqual(result["provenance"]["input_sha256"], config["data_hash"])
        self.assertEqual(result["provenance"]["input_source"], self.database.name)

    def test_cli_config_validates_budget_cv_and_new_paths(self):
        config = cli.make_config(
            self.database, self.output, seed=7,
            bootstrap_replicates=5000, cv_repeats=2, cv_folds=3, max_seconds=7200,
        )
        self.assertEqual(config["mode"], "controlled_comparison")
        self.assertEqual((config["cv_repeats"], config["cv_folds"]), (2, 3))
        self.assertFalse(config["input_synthetic"])
        self.assertNotIn("predictions", config)
        self.assertEqual(len(config["data_hash"]), 64)

        with self.assertRaisesRegex(ValueError, "cv_repeats"):
            cli.make_config(self.database, self.output, seed=1, bootstrap_replicates=100,
                            cv_repeats=0, cv_folds=3, max_seconds=10)
        with self.assertRaisesRegex(ValueError, "cv_folds"):
            cli.make_config(self.database, self.output, seed=1, bootstrap_replicates=100,
                            cv_repeats=2, cv_folds=1, max_seconds=10)
        with self.assertRaisesRegex(ValueError, "cv_repeats"):
            cli.make_config(self.database, self.output, seed=1, bootstrap_replicates=100,
                            cv_repeats=6, cv_folds=3, max_seconds=10)
        with self.assertRaisesRegex(ValueError, "cv_folds"):
            cli.make_config(self.database, self.output, seed=1, bootstrap_replicates=100,
                            cv_repeats=2, cv_folds=6, max_seconds=10)
        project = self.root / "existing.swa"
        project.mkdir()
        with self.assertRaisesRegex(FileExistsError, "Project path already exists"):
            cli.run(self.database, self.output, project, bootstrap_replicates=100,
                    cv_repeats=1, cv_folds=2, max_seconds=10)

    def test_controlled_comparison_is_faceted_and_only_joint_uses_joint_ci(self):
        result = controlled_envelope()
        joint = make_academic_figure(result, kind="comparison", metric="joint_mae_min", lang="ko")
        self.assertEqual([axis.get_title() for axis in joint.axes],
                         ["CMI", "MESA", "전체(출처 균등)"])
        self.assertTrue(all(axis.get_ylim()[0] == 0 for axis in joint.axes))
        self.assertGreater(joint.axes[0].get_ylim()[1], 72.7)
        self.assertTrue(all(len(axis.patches) == 2 for axis in joint.axes))
        self.assertTrue(all(any(isinstance(item, ErrorbarContainer) for item in axis.containers)
                            for axis in joint.axes))
        cmi_labels = [tick.get_text() for tick in joint.axes[0].get_xticklabels()]
        self.assertEqual(cmi_labels, ["개별 학습 data", "공통 학습 data"])
        footer = "\n".join(text.get_text() for text in joint.texts)
        self.assertIn("joint MAE", footer)
        self.assertIn("재사용 Test", footer)

        onset = make_academic_figure(result, kind="comparison", metric="onset_mae_min", lang="ko")
        self.assertTrue(all(not any(isinstance(item, ErrorbarContainer) for item in axis.containers)
                            for axis in onset.axes))
        onset_footer = "\n".join(text.get_text() for text in onset.texts)
        self.assertIn("joint MAE CI를 재사용하지 않음", onset_footer)

    def test_paired_difference_uses_increase_sign_and_localized_model_labels(self):
        result = controlled_envelope()
        figure = make_academic_figure(result, kind="paired_difference", lang="ko")
        axis = figure.axes[0]
        self.assertEqual([tick.get_text() for tick in axis.get_yticklabels()],
                         ["CMI", "MESA", "전체(출처 균등)"])
        points = [line.get_xdata()[0] for line in axis.lines if line.get_marker() == "o"]
        self.assertEqual(points, [.7, .7, .7])
        self.assertIn("양수는 악화", axis.get_xlabel())
        self.assertEqual(model_label("cmi.separate_data", "ko"), "CMI · 개별 학습 data")

    def test_dispatch_rejects_database_changed_after_submission(self):
        config = cli.make_config(
            self.database, self.output, seed=1,
            bootstrap_replicates=100, cv_repeats=1, cv_folds=2, max_seconds=10,
        )
        self.database.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            academic_jobs.execute(config, self.root)

    def test_native_table_export_identifies_units_reused_test_and_conditional_ci(self):
        result = controlled_envelope()
        result["raw"]["status"] = "retrospective_reused_holdout"
        result["provenance"]["evidence"] = "matched_retraining_reused_holdout"
        result["warnings"] = ["Reused Test; not independent external validation."]
        target = self.root / "controlled.csv"
        export_academic_tables(result, target, lang="en")
        summary = pd.read_csv(target)
        metadata = pd.read_csv(self.root / "controlled_metadata.csv").iloc[0]
        warnings = pd.read_csv(self.root / "controlled_warnings.csv")
        self.assertEqual(len(summary), 6)
        self.assertEqual(metadata["mode"], "controlled_comparison")
        self.assertEqual(metadata["metric_unit"], "minutes")
        self.assertEqual(metadata["test_reuse"], "reused historical Test")
        self.assertIn("conditional on fitted models", metadata["ci_scope"])
        self.assertFalse(bool(metadata["original_frozen_models_replaced"]))
        self.assertIn("Reused Test", warnings.loc[0, "warning"])

    def test_normalized_view_preserves_reused_scope_units_and_joint_conditional_ci(self):
        result = controlled_envelope()
        record = {
            "id": "controlled-1", "name": "controlled fixture", "status": "completed",
            "config": {"mode": "controlled_comparison", "data_hash": "db-sha",
                       "input_synthetic": False},
            "result": result,
        }
        view = adapt_run(record, "sleepwell")
        evaluation = view["evaluation"]
        self.assertEqual(evaluation["calculation"], "historical_reuse")
        self.assertEqual(evaluation["population"], "cmi_mesa_reused_test")
        self.assertEqual(evaluation["scope_basis"], "verified_same_run_aggregate_test_scope")
        self.assertIsNotNone(evaluation["scope_hash"])
        self.assertEqual(evaluation["averaging"], "equal_participant_with_equal_cohort_macro")
        self.assertEqual(evaluation["split_id"], "test")
        self.assertFalse(evaluation["scope_identifiers_exported"])
        self.assertEqual(evaluation["aggregate_n_nights"], 2848)
        self.assertEqual(evaluation["aggregate_n_participants"], 359)
        joint = next(m for m in view["metrics"]
                     if m["model_id"] == "cmi.separate_data" and m["metric"] == "joint_mae_min")
        onset = next(m for m in view["metrics"]
                     if m["model_id"] == "cmi.separate_data" and m["metric"] == "onset_mae_min")
        self.assertEqual(joint["unit"], "min")
        self.assertTrue(joint["variation"]["is_confidence_interval"])
        self.assertFalse(joint["variation"]["includes_training_selection_uncertainty"])
        self.assertIsNone(onset["variation"])
        self.assertEqual(compare(view, view, "joint_mae_min", "cmi.separate_data")["status"],
                         "reference_only")

    def test_native_report_describes_aggregate_scope_without_zero_sample_claim(self):
        sections = ReportService(self.root)._sections(
            "comparison", "ko", "question",
            {"results_summary": ["8개 모델의 cohort별 joint MAE와 95% CI"]},
            [],
            [{"run_id": "controlled-1", "result_hash": "result-sha", "data_hash": "db-sha", "evaluation": {
                "task": "sleep_timing", "target": "sleep_onset_and_wake",
                "evaluation_type": "controlled_comparison", "calculation": "historical_reuse",
                "population": "cmi_mesa_reused_test", "split_id": "test", "scope_ids": [],
                "scope_hash": "aggregate-scope", "scope_basis": "verified_same_run_aggregate_test_scope",
                "aggregate_n_nights": 2848, "aggregate_n_participants": 359,
            }}],
        )
        design = next(section for section in sections if section["section_id"] == "evaluation_design")
        self.assertIn("split_id=test", design["paragraphs"][0])
        self.assertIn("evaluation_scope_identifiers=not_exported", design["paragraphs"][0])
        self.assertIn("aggregate_n_nights=2848", design["paragraphs"][0])
        self.assertNotIn("evaluation_scope_n=0", design["paragraphs"][0])
        results = next(section for section in sections if section["section_id"] == "results")
        self.assertEqual(results["paragraphs"], ["8개 모델의 cohort별 joint MAE와 95% CI"])


if __name__ == "__main__":
    unittest.main()
