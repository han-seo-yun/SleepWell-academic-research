from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from researchbench import academic_jobs
from researchbench.academic_plotting import export_academic_tables, make_academic_figure
from researchbench.result_adapter import adapt_run


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compare_cmi_mesa_personalization", ROOT / "scripts" / "compare_cmi_mesa_personalization.py"
)
cli = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cli)
ARMS = ("data33", "data33_residual", "hybrid39", "hybrid39_personalized")


def envelope() -> dict:
    metrics = []
    for cohort, prefix, offset, people, nights in (
        ("CMI", "cmi", 0.0, 269, 4627),
        ("MESA", "mesa", 10.0, 2139, 14761),
        ("MACRO", "macro", 5.0, 2408, 19388),
    ):
        for index, arm in enumerate(ARMS):
            value = 55.0 + offset - index / 10
            metrics.append({
                "model_id": f"{prefix}.{arm}", "model": arm, "cohort": cohort,
                "split": "out_of_fold", "onset_mae_min": value + 1,
                "wake_mae_min": value - 1, "joint_mae_min": value,
                "ci95_low": value - 2, "ci95_high": value + 2,
                "n_participants": people, "n_nights": nights,
            })
    paired = [{
        "reference": "data33", "model": "data33_residual", "cohort": cohort,
        "improvement_min": 0.5, "ci95_low": 0.1, "ci95_high": 0.9,
    } for cohort in ("CMI", "MESA", "MACRO")]
    raw = {
        "schema": "sleepwell.personalization_comparison.v1",
        "status": "retrospective_grouped_cv",
        "verification": {"participant_overlap": 0, "oof_once_per_repeat": True, "same_nights_all_models": True},
        "primary_pair": ["data33", "data33_residual"],
        "metrics": metrics, "paired_comparisons": paired,
        "fold_metrics": [{"model": "data33"}], "repeat_metrics": [{"model": "data33"}],
        "history_metrics": [{"model": "data33", "history_band": "0"}],
        "history_paired_comparisons": [],
        "personalization_diagnostics": [{
            "cohort": "CMI", "history_band": "0", "zero_candidate_fraction": 1.0,
            "residual_active_fraction": 0.0,
        }],
        "selection": {
            "recommended_model": "data33", "rule": {"minimum_macro_improvement_min": 1.0},
            "candidates": [{"model": "data33_residual", "eligible": False}],
        },
        "limitations": ["Internal validation only."],
    }
    return {
        "mode": "personalization_comparison", "raw": raw, "summary": metrics,
        "warnings": raw["limitations"],
        "provenance": {"input_synthetic": False, "evidence": "retrospective_grouped_cv"},
    }


class PersonalizationComparisonIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "cmi_mesa.sqlite3"
        self.database.write_bytes(b"personalization comparison database")
        self.output = self.root / "output"

    def tearDown(self):
        self.temporary.cleanup()

    def test_config_dispatch_and_database_identity(self):
        config = cli.make_config(
            self.database, self.output, seed=20261106, bootstrap_replicates=100,
            cv_repeats=2, cv_folds=5, max_seconds=30,
        )
        evaluator = mock.Mock(return_value=envelope())
        module = types.ModuleType("researchbench.personalization_comparison")
        module.run_comparison = evaluator
        with mock.patch.dict(sys.modules, {"researchbench.personalization_comparison": module}):
            result = academic_jobs.execute(config, self.root)
        self.assertEqual(evaluator.call_args.args[0]["database_path"], str(self.database.resolve()))
        self.assertFalse(evaluator.call_args.args[0]["input_synthetic"])
        self.assertEqual(result["provenance"]["input_sha256"], config["data_hash"])
        self.database.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Retraining database SHA256 mismatch"):
            academic_jobs.execute(config, self.root)

    def test_runner_locks_before_queue_submission(self):
        events = []

        class FakeProject:
            @classmethod
            def create(cls, path, name):
                return cls()

            def runs(self):
                return [{"id": "run-1", "status": "completed", "result": envelope()}]

        class FakeQueue:
            def __init__(self, project):
                self.busy, self.active = False, None

            def submit(self, config):
                events.append(("submit", (Path(config["output_dir"]) / "procedure_lock.json").is_file()))
                return "run-1"

            def close(self):
                pass

        module = types.ModuleType("researchbench.personalization_comparison")

        def prepare(config):
            output = Path(config["output_dir"])
            output.mkdir()
            (output / "procedure_lock.json").write_text("{}", encoding="utf-8")
            events.append(("prepare", True))

        module.prepare_comparison = prepare
        with mock.patch.dict(sys.modules, {"researchbench.personalization_comparison": module}), \
                mock.patch.object(cli, "AcademicProject", FakeProject), \
                mock.patch.object(cli, "AcademicQueue", FakeQueue):
            record = cli.run(self.database, self.output, self.root / "project.swa", bootstrap_replicates=100, max_seconds=30)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(events, [("prepare", True), ("submit", True)])

    def test_normalized_view_plot_and_export_are_grouped_oof(self):
        result = envelope()
        record = {
            "id": "personalization-1", "name": "comparison", "status": "completed",
            "config": {"mode": "personalization_comparison", "data_hash": "db-sha", "input_synthetic": False},
            "result": result,
        }
        view = adapt_run(record, "sleepwell")
        self.assertEqual(view["evaluation"]["calculation"], "retrospective_grouped_cv")
        self.assertEqual(view["evaluation"]["split_id"], "out_of_fold")
        metric = next(row for row in view["metrics"] if row["model_id"] == "macro.data33" and row["metric"] == "joint_mae_min")
        self.assertTrue(metric["variation"]["conditional_on_fixed_procedure"])
        figure = make_academic_figure(result, kind="comparison", lang="ko")
        self.assertEqual([axis.get_title() for axis in figure.axes], ["CMI", "MESA", "전체(출처 균등)"])
        self.assertTrue(all(len(axis.patches) == len(ARMS) for axis in figure.axes))
        target = self.root / "comparison.csv"
        export_academic_tables(result, target, lang="ko")
        metadata = pd.read_csv(self.root / "comparison_실행_메타데이터.csv").iloc[0]
        self.assertEqual(metadata["mode"], "personalization_comparison")
        self.assertIn("not a Test holdout", metadata["test_reuse"])
        self.assertTrue((self.root / "comparison_발표_모델_선정.csv").is_file())
        self.assertTrue((self.root / "comparison_선정_후보_비교.csv").is_file())
        self.assertTrue((self.root / "comparison_개인화_진단.csv").is_file())
        self.assertFalse((self.root / "comparison_미사용_MESA_holdout_성능.csv").exists())


if __name__ == "__main__":
    unittest.main()
