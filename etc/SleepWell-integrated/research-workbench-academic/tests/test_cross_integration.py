from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from researchbench import academic_jobs
from researchbench.academic_plotting import export_academic_tables, make_academic_figure, model_label
from researchbench.result_adapter import adapt_run


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evaluate_cmi_mesa_transfer", ROOT / "scripts" / "evaluate_cmi_mesa_transfer.py",
)
cli = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cli)


def cross_envelope() -> dict:
    models = ("cmi_data", "mesa_data", "pooled_data", "mean50_data",
              "known_source_data", "recent_median")
    summary = []
    for cohort, prefix, offset, people, nights in (
        ("CMI", "cmi", 0.0, 44, 673),
        ("MESA", "mesa", 20.0, 315, 2175),
        ("MACRO", "macro", 10.0, 359, 2848),
    ):
        for index, model in enumerate(models):
            joint = 48.0 + offset + index
            summary.append({
                "model_id": f"{prefix}.{model}", "model": model, "cohort": cohort,
                "split": "test", "onset_mae_min": joint + 1,
                "wake_mae_min": joint - 1, "joint_mae_min": joint,
                "ci95_low": joint - 2, "ci95_high": joint + 2,
                "n_participants": people, "n_nights": nights,
            })
    paired = [{
        "reference": "recent_median", "model": "mean50_data", "cohort": cohort,
        "improvement_min": -1.0, "ci95_low": -2.0, "ci95_high": .1,
    } for cohort in ("CMI", "MESA", "MACRO")]
    return {
        "mode": "cross_cohort_evaluation",
        "raw": {
            "schema": "sleepwell.cross_cohort_evaluation.v1",
            "status": "retrospective_reused_holdout",
            "verification": {"database_unchanged": True},
            "primary_pair": ["recent_median", "mean50_data"],
            "paired_comparisons": paired,
        },
        "summary": summary,
        "warnings": ["Not independent general-user validation."],
        "provenance": {"input_synthetic": False, "evidence": "frozen_cross_cohort_reused_holdout"},
    }


class CrossCohortIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "cmi_mesa.sqlite3"
        self.database.write_bytes(b"cross fixture database")
        self.source = self.root / "source"
        self.source.mkdir()
        for name in cli.REQUIRED_SOURCE_ARTIFACTS:
            (self.source / name).write_bytes(b"fixture")
        self.output = self.root / "output"

    def tearDown(self):
        self.temporary.cleanup()

    def test_cli_config_and_dispatch_preserve_frozen_source_and_database_identity(self):
        config = cli.make_config(
            self.database, self.source, self.output,
            seed=20261105, bootstrap_replicates=100, max_seconds=30,
        )
        envelope = cross_envelope()
        evaluator = mock.Mock(return_value=envelope)
        module = types.ModuleType("researchbench.cross_evaluation")
        module.run_cross_evaluation = evaluator
        with mock.patch.dict(sys.modules, {"researchbench.cross_evaluation": module}):
            result = academic_jobs.execute(config, self.root)
        supplied = evaluator.call_args.args[0]
        self.assertEqual(config["mode"], "cross_cohort_evaluation")
        self.assertEqual(supplied["database_path"], str(self.database.resolve()))
        self.assertEqual(supplied["source_run_dir"], str(self.source.resolve()))
        self.assertFalse(supplied["input_synthetic"])
        self.assertEqual(result["provenance"]["input_sha256"], config["data_hash"])
        self.assertEqual(result["provenance"]["input_source"], self.database.name)

    def test_cli_rejects_incomplete_source_run_and_existing_output(self):
        (self.source / "selection_lock.json").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "selection_lock.json"):
            cli.make_config(
                self.database, self.source, self.output,
                seed=1, bootstrap_replicates=100, max_seconds=10,
            )
        (self.source / "selection_lock.json").write_bytes(b"fixture")
        self.output.mkdir()
        with self.assertRaisesRegex(FileExistsError, "Output path already exists"):
            cli.make_config(
                self.database, self.source, self.output,
                seed=1, bootstrap_replicates=100, max_seconds=10,
            )

    def test_workbench_view_plot_and_export_identify_frozen_cross_evaluation(self):
        result = cross_envelope()
        record = {
            "id": "cross-1", "status": "completed",
            "config": {"mode": "cross_cohort_evaluation", "data_hash": "db-sha",
                       "input_synthetic": False},
            "result": result,
        }
        view = adapt_run(record, "sleepwell")
        self.assertEqual(view["evaluation"]["calculation"], "historical_reuse")
        self.assertEqual(view["evaluation"]["population"], "cmi_mesa_reused_test")
        self.assertEqual(view["evaluation"]["scope_basis"],
                         "verified_same_run_aggregate_test_scope")
        self.assertEqual(model_label("cmi.mean50_data", "ko"),
                         "CMI · CMI·MESA 50:50 평균 data")

        figure = make_academic_figure(result, kind="comparison", lang="ko")
        self.assertEqual([axis.get_title() for axis in figure.axes],
                         ["CMI", "MESA", "전체(출처 균등)"])
        self.assertTrue(all(len(axis.patches) == 6 for axis in figure.axes))
        footer = "\n".join(text.get_text() for text in figure.texts)
        self.assertIn("동결 모델 교차 평가", footer)
        self.assertIn("독립 외부·일반 사용자 검증 아님", footer)

        target = self.root / "cross.csv"
        export_academic_tables(result, target, lang="ko")
        metadata = pd.read_csv(self.root / "cross_실행_메타데이터.csv").iloc[0]
        self.assertEqual(metadata["mode"], "cross_cohort_evaluation")
        self.assertEqual(metadata["test_reuse"], "reused historical Test")
        self.assertFalse(bool(metadata["original_frozen_models_replaced"]))


if __name__ == "__main__":
    unittest.main()
