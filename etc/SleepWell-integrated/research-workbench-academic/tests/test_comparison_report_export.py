from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from researchbench.reporting import ReportService
from researchbench.result_adapter import adapt_run, digest


def _record() -> dict:
    models = {
        "selected_alpha": (2.0, -1.0, 3.0, -2.0),
        "selected_beta": (8.0, -7.0, 6.0, -5.0),
        "excluded_secret_model": (0.0, 0.0, 0.0, 0.0),
    }
    truths = [("raw-person-A", "night-1", 100.0), ("raw-person-A", "night-2", 120.0),
              ("raw-person-B", "night-1", 140.0), ("raw-person-B", "night-2", 160.0)]
    predictions = []
    summaries = []
    for model, errors in models.items():
        for (participant, cutoff, actual), error in zip(truths, errors):
            predictions.append({
                "participant_id": participant, "cutoff": cutoff, "model_id": model,
                "actual_onset_offset_min": actual, "onset_offset_min": actual + error,
                "actual_wake_offset_min": actual + 480, "wake_offset_min": actual + 480 + error,
                "actual_interval_min": 480, "interval_min": 480,
            })
        summaries.append({"model_id": model, "onset_mae_min": sum(abs(x) for x in errors) / 4,
                          "onset_rmse_min": (sum(x*x for x in errors) / 4) ** .5,
                          "n_nights": 4, "n_participants": 2})
    return {
        "id": "comparison_export_run", "name": "Comparison export fixture", "status": "completed",
        "config": {"mode": "report", "data_hash": "fixture-data", "input_synthetic": False},
        "result": {"mode": "report", "summary": summaries, "raw": {"predictions": predictions},
                   "provenance": {"executed": True, "device": "CPU",
                                  "source_model": r"C:\Users\private-account\models\sleepwell.model"}},
    }


class ComparisonReportExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "comparison.swa"
        (self.project / "runs").mkdir(parents=True)
        self.record = _record()
        (self.project / "runs" / f"{self.record['id']}.json").write_text(
            json.dumps(self.record, ensure_ascii=False), encoding="utf-8")
        self.view = adapt_run(self.record, "sleepwell", self.project)
        self.service = ReportService(self.project)
        self.selection = {
            "run_id": self.record["id"], "result_hash": digest(self.record),
            "model_ids": ["selected_alpha", "selected_beta"], "target": "onset",
            "averaging": "participant_equal", "tolerance": 5.0,
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_checked_package_contains_current_aggregate_and_exact_seven_figures(self) -> None:
        report = self.service.create_comparison(
            self.view, self.selection, lang="en", question="Which selected timing model is preferable?")
        self.assertEqual(self.service.check(report)["state"], "automatic_check_passed")
        formats = ["md", "json", "csv", "figures"]
        if importlib.util.find_spec("reportlab") and importlib.util.find_spec("pypdf"):
            formats.append("pdf")
        paths = self.service.export(
            report["report_id"], self.root / "exports",
            formats=formats, mode="checked")

        figure_names = sorted(path.name for path in paths["figures"].iterdir())
        expected_stems = [f"comparison_{index:02d}_{kind}" for index, kind in enumerate(
            ("mae", "rmse", "bias", "tolerance", "predictions", "errors", "participants"), start=1)]
        self.assertEqual(figure_names, sorted(f"{stem}.{suffix}" for stem in expected_stems
                                             for suffix in ("png", "pdf", "svg")))
        svg_text = "\n".join((paths["figures"] / f"{stem}.svg").read_text(encoding="utf-8")
                             for stem in expected_stems)
        self.assertNotIn("excluded_secret_model", svg_text)

        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        self.assertEqual(manifest["comparison_selection"], self.selection)
        self.assertEqual(manifest["comparison_figure_kinds"],
                         ["mae", "rmse", "bias", "tolerance", "predictions", "errors", "participants"])
        self.assertEqual(len(manifest["files"]["figures"]["items"]), 21)
        self.assertTrue(all(len(value) == 64 for value in manifest["files"]["figures"]["items"].values()))
        for module in ("comparison_reporting.py", "comparison_metrics.py", "comparison_plotting.py"):
            self.assertIn(module, manifest["generation_code"]["files_sha256"])

        audit_text = paths["audit_json"].read_text(encoding="utf-8")
        self.assertNotIn("raw-person-A", audit_text)
        self.assertNotIn("raw-person-B", audit_text)
        self.assertNotIn("excluded_secret_model", audit_text)
        self.assertNotIn(str(self.project), audit_text)
        self.assertNotIn("private-account", audit_text)
        self.assertIn("[local source]", audit_text)
        audit = json.loads(audit_text)
        self.assertEqual(audit["report"]["artifact_sha256"], report["artifact_sha256"])
        self.assertFalse(audit["report"]["local_source_paths_included"])
        summary = audit["report"]["comparison_evidence"]["summary"]
        self.assertFalse(summary["raw_prediction_rows_included"])
        self.assertFalse(summary["participant_identifiers_included"])
        self.assertEqual([row["model_id"] for row in summary["rows"]], self.selection["model_ids"])

        with paths["tables_csv"].open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        metric_rows = [row for row in rows if row["claim_type"] == "comparison_metric_fact"]
        self.assertEqual(len(metric_rows), 8)
        self.assertEqual({row["model_id"] for row in metric_rows}, set(self.selection["model_ids"]))
        markdown = paths["markdown"].read_text(encoding="utf-8")
        self.assertIn("selected_alpha", markdown)
        self.assertIn("within_tolerance_percent", markdown)
        self.assertNotIn("excluded_secret_model", markdown)

        if "pdf" in paths:
            from pypdf import PdfReader
            reader = PdfReader(str(paths["pdf"]))
            pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
            self.assertIn("selected_alpha", pdf_text)
            self.assertIn("selected_beta", pdf_text)
            self.assertIn("MAE (min)", pdf_text)
            self.assertIn("RMSE (min)", pdf_text)
            self.assertIn("Within", pdf_text)
            self.assertNotIn("excluded_secret_model", pdf_text)
            self.assertNotIn("In the selected comparison", pdf_text)
            for claim in report["claims"]:
                if claim["claim_type"] == "comparison_metric_fact":
                    self.assertNotIn(claim["claim_id"], pdf_text)
            image_count = sum(len((page.get("/Resources") or {}).get("/XObject") or {}) for page in reader.pages)
            self.assertGreaterEqual(image_count, 7)


if __name__ == "__main__":
    unittest.main()
