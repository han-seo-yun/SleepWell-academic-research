import gc
import tempfile
import time
import tkinter as tk
import unittest
import uuid
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

from researchbench.academic_store import AcademicProject
from researchbench.knowledge_ui import KnowledgeUIMixin
from researchbench.evidence_library import EvidenceLibrary
from researchbench.reporting import ReportService
from researchbench.result_adapter import adapt_run


class SimpleReportHost(KnowledgeUIMixin):
    def __init__(self, root, project, views, lang="ko"):
        self.root = root
        self.project = project
        self.lang = lang
        self.vars = {}
        self.status = tk.StringVar(root, value="")
        self.views = views
        self.selected_run = views[-1]["run_id"] if views else None

    def var(self, key, default=""):
        if key not in self.vars:
            self.vars[key] = tk.StringVar(self.root, value=default)
        return self.vars[key]

    def safe(self, function):
        return function()

    def platform_runs(self):
        return list(self.views)


class SimpleReportsTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk unavailable: {error}")
        self.root.withdraw()
        self.temp = tempfile.TemporaryDirectory()
        self.project = AcademicProject.create(Path(self.temp.name) / "simple.swa", "Simple")
        record = {
            "id": str(uuid.uuid4()), "name": "Measured run", "status": "completed",
            "config": {
                "mode": "personalization", "data_hash": "source-hash", "input_synthetic": False,
                "selection": {
                    "source_rows": 120, "rows": 2, "participants": 1, "cohort": "all",
                    "actual_cohort_participants": {"CMI": 1},
                    "split": "train", "max_nights_per_participant": 2,
                    "selection_rule": "first_nights_per_participant",
                    "purpose": "exploratory_model_experiment_not_independent_validation",
                    "source_sha256": "source-sha256",
                },
            },
            "created": "2026-01-01T00:00:00+00:00", "duration": 1.0,
            "result": {"mode": "personalization", "summary": [{
                "model_id": "cmi.data", "joint_mae_min": 12.5,
                "n_nights": 2, "n_participants": 1,
            }], "raw": {"predictions": []}, "provenance": {"executed": True}},
        }
        self.project.save_run(record)
        self.view = adapt_run(record, "sleepwell", self.project.path)
        self.host = SimpleReportHost(self.root, self.project, [self.view])

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.host = None
        gc.collect()
        self.temp.cleanup()

    def frame(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True)
        return frame

    def citation(self):
        source = Path(self.temp.name) / "guideline.md"
        source.write_text("# Guidance\n\nReport model performance with confidence intervals.", encoding="utf-8")
        registered = self.host._evidence_import_sync(source, {
            "title": "Guidance", "version": "1", "language": "en",
        })
        result = self.host._evidence_search_sync("model performance confidence intervals", {
            "lang": "en", "top_k": 1,
        })
        hit = dict(result["hits"][0])
        hit["search_id"] = result["search_id"]
        return registered, hit

    def test_simple_page_offers_metric_run_and_separate_gradual_source(self):
        self.host.build_simple_reports(self.frame())
        values = list(self.host.simple_report_source_combo.cget("values"))
        self.assertEqual(len(values), 3)
        self.assertIn("Measured run", values[1])
        self.assertIn("gradual_v1", values[2])
        self.assertEqual(self.host.var("knowledge_simple_report_source").get(), self.view["run_id"])

    def test_run_report_keeps_run_metrics_and_retrieval_cannot_replace_them(self):
        _registered, citation = self.citation()
        destination = Path(self.temp.name) / "run.pdf"
        with patch("researchbench.guideline_retrieval.prepare_guideline_retrieval",
                   return_value={"citations": [citation]}) as retrieve:
            result = self.host._simple_report_generate_sync(
                self.view, "현재 실험 성능", destination, "ko"
            )
        retrieve.assert_called_once_with(self.project.path)
        report = result["report"]
        self.assertEqual(report["template"], "research")
        self.assertEqual([item["run_id"] for item in report["run_refs"]], [self.view["run_id"]])
        metric_claims = [item for item in report["claims"] if item["evidence"]["kind"] == "experiment"]
        self.assertTrue(metric_claims)
        self.assertTrue(any(item["evidence"]["value"] == 12.5 for item in metric_claims))
        self.assertTrue(any(item["evidence"]["kind"] == "literature" for item in report["claims"]))
        self.assertEqual(report["automatic_check"]["state"], "automatic_check_passed")
        self.assertTrue(destination.is_file())

    def test_research_sections_use_verified_selected_scope_without_placeholders(self):
        _registered, citation = self.citation()
        destination = Path(self.temp.name) / "scope.pdf"
        with patch("researchbench.guideline_retrieval.prepare_guideline_retrieval",
                   return_value={"citations": [citation]}):
            report = self.host._simple_report_generate_sync(
                self.view, "", destination, "ko"
            )["report"]

        selected_scope = report["run_refs"][0]["selected_scope"]
        self.assertEqual(selected_scope, self.view["config"]["selection"])
        self.assertEqual(report["run_refs"][0]["experiment_mode"], "personalization")
        rendered = "\n".join(
            str(paragraph)
            for section in report["sections"]
            for paragraph in section.get("paragraphs", [])
        )
        self.assertNotIn("제공되지 않음", rendered)
        self.assertIn("rows=2", rendered)
        self.assertIn("participants=1", rendered)
        self.assertIn("actual_cohort_participants=CMI:1", rendered)
        self.assertNotIn("MESA:", rendered)
        self.assertIn("source_sha256=source-sha256", rendered)
        self.assertIn("실제 관측 자료", rendered)
        self.assertIn("독립 검증이 아닙니다", rendered)

        from pypdf import PdfReader
        pdf_text = "\n".join(page.extract_text() for page in PdfReader(destination).pages)
        self.assertIn("actual_cohort_participants=CMI:1", pdf_text)

        report["run_refs"][0]["selected_scope"]["rows"] = 99
        with EvidenceLibrary(self.project.path) as library:
            check = ReportService(self.project.path, library).check(report)
        self.assertIn("selected_scope_mismatch", {item["code"] for item in check["errors"]})

    def test_gradual_selection_never_uses_selected_run(self):
        _registered, citation = self.citation()
        destination = Path(self.temp.name) / "gradual.pdf"
        with patch("researchbench.guideline_retrieval.prepare_guideline_retrieval",
                   return_value={"citations": [citation]}):
            result = self.host._simple_report_generate_sync(None, "", destination, "ko")
        report = result["report"]
        self.assertEqual(report["template"], "gradual_guidelines")
        self.assertEqual(report["run_refs"], [])
        self.assertTrue(any(item["evidence"]["kind"] == "gradual_metric" for item in report["claims"]))

    def test_cancelled_save_creates_no_report_and_completion_survives_navigation(self):
        frame = self.frame()
        self.host.build_simple_reports(frame)
        with patch("researchbench.knowledge_ui.filedialog.asksaveasfilename", return_value=""):
            self.host.simple_report_create_button.invoke()
        self.assertEqual(self.host._list_reports_sync(), [])
        self.assertFalse(getattr(self.host, "_knowledge_worker_active", False))

        _registered, citation = self.citation()
        destination = Path(self.temp.name) / "async.pdf"
        with patch("researchbench.knowledge_ui.filedialog.asksaveasfilename", return_value=str(destination)), \
             patch("researchbench.guideline_retrieval.prepare_guideline_retrieval",
                   return_value={"citations": [citation]}):
            self.host.simple_report_create_button.invoke()
            frame.destroy()
            deadline = time.monotonic() + 20
            while getattr(self.host, "_knowledge_worker_active", False) and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
        self.assertFalse(self.host._knowledge_worker_active)
        self.assertTrue(destination.is_file(), self.host.status.get())
        self.assertEqual(self.host.var("knowledge_pdf_path").get(), str(destination))

    def test_diagnostic_view_without_metrics_is_not_offered(self):
        diagnostic = dict(self.view, run_id="diagnostic", name="Diagnostics", metrics=[])
        host = SimpleReportHost(self.root, self.project, [diagnostic, self.view])
        host.selected_run = "diagnostic"
        host.build_simple_reports(self.frame())
        values = list(host.simple_report_source_combo.cget("values"))
        self.assertEqual(len(values), 3)
        self.assertNotIn("Diagnostics", " ".join(values))
        self.assertEqual(host.var("knowledge_simple_report_source").get(), "")
        self.assertEqual(str(host.simple_report_create_button.cget("state")), "disabled")

    def test_invalid_selected_run_surfaces_error_without_gradual_fallback(self):
        _registered, citation = self.citation()
        invalid = dict(self.view, result_hash="changed-hash")
        destination = Path(self.temp.name) / "must-not-exist.pdf"
        with patch("researchbench.guideline_retrieval.prepare_guideline_retrieval",
                   return_value={"citations": [citation]}):
            with self.assertRaises(ValueError):
                self.host._simple_report_generate_sync(invalid, "", destination, "ko")
        self.assertFalse(destination.exists())
        self.assertEqual(self.host._list_reports_sync(), [])


if __name__ == "__main__":
    unittest.main()
