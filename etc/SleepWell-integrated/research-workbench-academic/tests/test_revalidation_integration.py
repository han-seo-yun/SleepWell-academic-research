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
    "revalidate_cmi_mesa", ROOT / "scripts" / "revalidate_cmi_mesa.py"
)
cli = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cli)


MODELS = (
    "cmi_data", "mesa_data", "pooled_data", "mean50_data", "known_source_data",
    "recent_median", "cmi_hybrid", "mesa_hybrid", "pooled_hybrid", "mean50_hybrid",
    "known_source_hybrid", "recent_median_cmi", "recent_median_mesa",
    "pooled_source_data", "pooled_source_hybrid", "separate_shared_data",
    "separate_shared_hybrid",
)


def revalidation_envelope() -> dict:
    metrics = []
    for cohort, prefix, offset, people, nights in (
        ("CMI", "cmi", 0.0, 269, 4627),
        ("MESA", "mesa", 20.0, 2139, 14761),
        ("MACRO", "macro", 10.0, 2408, 19388),
    ):
        for index, model in enumerate(MODELS):
            value = 48.0 + offset + index / 10
            metrics.append({
                "model_id": f"{prefix}.{model}", "model": model, "cohort": cohort,
                "split": "out_of_fold", "onset_mae_min": value + 1,
                "wake_mae_min": value - 1, "joint_mae_min": value,
                "ci95_low": value - 2, "ci95_high": value + 2,
                "n_participants": people, "n_nights": nights,
            })
    paired = [{
        "reference": "recent_median", "model": "pooled_data", "cohort": cohort,
        "improvement_min": 2.0, "ci95_low": 1.0, "ci95_high": 3.0,
    } for cohort in ("CMI", "MESA", "MACRO")]
    raw = {
        "schema": "sleepwell.participant_revalidation.v1",
        "status": "retrospective_grouped_cv",
        "verification": {
            "participant_overlap": 0, "oof_once_per_repeat": True,
            "same_nights_all_models": True,
        },
        "primary_pair": ["recent_median", "pooled_data"],
        "metrics": metrics,
        "paired_comparisons": paired,
        "fold_metrics": [{"repeat": 0, "fold": 0, "model": "pooled_data"}],
        "history_metrics": [{"history_band": "0", "model": "pooled_data"}],
        "history_paired_comparisons": [],
        "untouched_holdout_metrics": [{
            "model_id": "mesa.pooled_data", "model": "pooled_data", "cohort": "MESA",
            "split": "unused_mesa_holdout", "joint_mae_min": 61.0,
            "n_participants": 20, "n_nights": 133,
        }],
        "untouched_holdout_paired_comparisons": [],
        "limitations": ["Internal validation only."],
        "protocol": {"cv_folds": 5, "cv_repeats": 2},
        "artifacts": {},
    }
    return {
        "mode": "participant_revalidation", "raw": raw, "summary": metrics,
        "warnings": raw["limitations"],
        "provenance": {"input_synthetic": False, "evidence": "retrospective_grouped_cv"},
    }


class ParticipantRevalidationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "cmi_mesa.sqlite3"
        self.database.write_bytes(b"participant cv fixture database")
        self.audit = self.root / "participant_audit.json"
        self.audit.write_text(json.dumps({"unused_eligible_participants": 0}), encoding="utf-8")
        self.output = self.root / "output"
        self.holdout = self.root / "unused_mesa.csv"
        self.holdout.write_text("cohort,subject_id\nMESA,u1\n", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_config_and_dispatch_verify_database_and_participant_audit_identity(self):
        config = cli.make_config(
            self.database, self.audit, self.output, seed=20261106,
            bootstrap_replicates=100, cv_repeats=2, cv_folds=5, max_seconds=30,
        )
        evaluator = mock.Mock(return_value=revalidation_envelope())
        module = types.ModuleType("researchbench.participant_revalidation")
        module.run_participant_revalidation = evaluator
        with mock.patch.dict(sys.modules, {"researchbench.participant_revalidation": module}):
            result = academic_jobs.execute(config, self.root)
        supplied = evaluator.call_args.args[0]
        self.assertEqual(supplied["database_path"], str(self.database.resolve()))
        self.assertEqual(supplied["participant_audit_path"], str(self.audit.resolve()))
        self.assertEqual((supplied["cv_folds"], supplied["cv_repeats"]), (5, 2))
        self.assertFalse(supplied["input_synthetic"])
        self.assertEqual(result["provenance"]["input_sha256"], config["data_hash"])

        holdout_config = cli.make_config(
            self.database, self.audit, self.output, seed=20261106,
            bootstrap_replicates=100, cv_repeats=2, cv_folds=5, max_seconds=30,
            untouched_holdout=self.holdout,
        )
        self.assertEqual(holdout_config["untouched_holdout_path"], str(self.holdout.resolve()))
        self.holdout.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Untouched holdout SHA256 mismatch"):
            academic_jobs.execute(holdout_config, self.root)

        self.audit.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Participant audit SHA256 mismatch"):
            academic_jobs.execute(config, self.root)

    def test_run_writes_prefit_lock_before_queue_submission(self):
        events = []
        envelope = revalidation_envelope()

        class FakeProject:
            @classmethod
            def create(cls, path, name):
                events.append(("project", Path(path).exists()))
                return cls()

            def runs(self):
                return [{"id": "run-1", "status": "completed", "result": envelope}]

        class FakeQueue:
            def __init__(self, project):
                self.project, self.busy, self.active = project, False, None

            def submit(self, config):
                events.append(("submit", (Path(config["output_dir"]) / "protocol.json").is_file()))
                return "run-1"

            def close(self):
                pass

        module = types.ModuleType("researchbench.participant_revalidation")

        def prepare(config):
            output = Path(config["output_dir"])
            output.mkdir()
            (output / "protocol.json").write_text("{}", encoding="utf-8")
            events.append(("prepare", True))

        module.prepare_revalidation = prepare
        with mock.patch.dict(sys.modules, {"researchbench.participant_revalidation": module}), \
                mock.patch.object(cli, "AcademicProject", FakeProject), \
                mock.patch.object(cli, "AcademicQueue", FakeQueue):
            record = cli.run(
                self.database, self.audit, self.output, self.root / "project.swa",
                bootstrap_replicates=100, cv_repeats=2, cv_folds=5, max_seconds=30,
            )
        self.assertEqual(record["status"], "completed")
        self.assertEqual(events[0], ("prepare", True))
        self.assertEqual(events[-1], ("submit", True))

    def test_workbench_view_figure_and_export_describe_grouped_cv_without_test_claim(self):
        result = revalidation_envelope()
        record = {
            "id": "revalidation-1", "status": "completed",
            "config": {"mode": "participant_revalidation", "data_hash": "db-sha",
                       "input_synthetic": False},
            "result": result,
        }
        view = adapt_run(record, "sleepwell")
        evaluation = view["evaluation"]
        self.assertEqual(evaluation["calculation"], "retrospective_grouped_cv")
        self.assertEqual(evaluation["population"], "cmi_mesa_all_participant_grouped_cv")
        self.assertEqual(evaluation["split_id"], "out_of_fold")
        self.assertEqual(evaluation["scope_basis"], "verified_grouped_out_of_fold_scope")
        metric = next(row for row in view["metrics"]
                      if row["model_id"] == "macro.pooled_data" and
                      row["metric"] == "joint_mae_min")
        self.assertTrue(metric["variation"]["conditional_on_fixed_procedure"])
        self.assertFalse(metric["variation"]["conditional_on_fitted_model"])

        figure = make_academic_figure(result, kind="comparison", lang="ko")
        self.assertEqual([axis.get_title() for axis in figure.axes],
                         ["CMI", "MESA", "전체(출처 균등)"])
        self.assertTrue(all(len(axis.patches) == len(MODELS) for axis in figure.axes))
        footer = "\n".join(text.get_text() for text in figure.texts)
        self.assertIn("5-fold×2회 교차검증", footer)
        self.assertNotIn("재사용 Test", footer)

        target = self.root / "revalidation.csv"
        export_academic_tables(result, target, lang="ko")
        metadata = pd.read_csv(self.root / "revalidation_실행_메타데이터.csv").iloc[0]
        self.assertEqual(metadata["mode"], "participant_revalidation")
        self.assertIn("not a Test holdout", metadata["test_reuse"])
        self.assertTrue((self.root / "revalidation_Fold_성능.csv").is_file())
        self.assertTrue((self.root / "revalidation_미사용_MESA_holdout_성능.csv").is_file())


if __name__ == "__main__":
    unittest.main()
