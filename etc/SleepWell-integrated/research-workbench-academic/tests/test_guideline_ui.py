import gc
import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

from pypdf import PdfReader

from researchbench.academic_app import AcademicApp, PAGES
from researchbench.academic_store import AcademicProject


class GuidelineUITests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.temp = tempfile.TemporaryDirectory()
        self.project = AcademicProject.create(Path(self.temp.name) / "test.swa", "Guideline test")
        self.app = AcademicApp(self.root, self.project, language="ko", preferences=False)

    def tearDown(self):
        if getattr(self.app, "_guideline_busy", False):
            self.app.cancel_guideline_work()
            self.pump(lambda: not self.app._guideline_busy)
        self.root.update_idletasks()
        self.app.close()
        # Dispose Tk reference cycles on the creating thread before a later
        # test starts a PDF worker that may trigger Python's cyclic collector.
        self.app = None
        self.root = None
        gc.collect()
        self.temp.cleanup()

    def pump(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_blank_review_revision_reopen_and_language(self):
        self.assertNotIn("guidelines", PAGES)  # retained service, removed default menu
        self.app.navigate("guidelines")
        self.app._create_guideline_audit(False)
        original_id = self.app.var("guideline_selected").get()
        self.assertEqual(len(self.app.guideline_current["tripod"]["items"]), 52)
        self.assertEqual(len(self.app.guideline_current["probast"]["questions"]), 34)
        self.app.guideline_tree.selection_set("tripod/1")
        self.app._select_guideline_item()
        self.app.var("guideline_reviewer").set("Research reviewer")
        self.app.guideline_note.delete("1.0", "end")
        self.app.guideline_note.insert("1.0", "Draft title reporting rationale")
        self.app.change_language("en")
        self.assertEqual(self.app.guideline_notebook.tab(0, "text"), "Performance reassessment")
        self.assertEqual(self.app.guideline_note.get("1.0", "end-1c"), "Draft title reporting rationale")
        self.app.guideline_response.current(self.app.guideline_choices.index("partially_satisfied"))
        self.app.var("guideline_title").set("Revised reporting assessment")
        self.app._save_guideline_item()
        new_id = self.app.var("guideline_selected").get()
        self.assertNotEqual(original_id, new_id)
        service = self.app._guideline_service()
        self.assertEqual(service.get(original_id)["tripod"]["items"][0]["status"], "pending")
        self.assertEqual(service.get(new_id)["tripod"]["items"][0]["status"], "partially_satisfied")
        self.assertEqual(service.get(new_id)["title"], "Revised reporting assessment")
        self.app.save()
        reopened = AcademicProject.open(self.project.path)
        self.assertEqual(reopened.state["ui"]["vars"]["guideline_selected"], new_id)

    def test_na_choices_and_project_switch_clear_draft(self):
        self.app.navigate("guidelines")
        self.app._create_guideline_audit(False)
        self.app.guideline_tree.selection_set("probast/evaluation:1.1")
        self.app._select_guideline_item()
        self.assertNotIn("NA", self.app.guideline_choices)
        self.app.guideline_tree.selection_set("probast/evaluation:4.6")
        self.app._select_guideline_item()
        self.assertIn("NA", self.app.guideline_choices)
        self.app.capture_guideline_drafts()
        other = AcademicProject.create(Path(self.temp.name) / "other.swa", "Other")
        self.app.attach(other)
        self.assertEqual(self.app.var("guideline_selected").get(), "")
        self.assertEqual(self.app.var("guideline_draft").get(), "")

    def test_worker_stays_responsive_cancellable_and_blocks_project_switch(self):
        started = threading.Event()
        result = []

        def work(progress, cancelled):
            started.set()
            while not cancelled():
                time.sleep(.01)
            return {"cancelled": True}

        self.app._guideline_async(work, result.append)
        self.pump(started.is_set)
        with self.assertRaises(ValueError):
            self.app.ensure_switch()
        self.app.navigate("guidelines")
        self.app.cancel_guideline_work()
        self.pump(lambda: not self.app._guideline_busy)
        self.assertEqual(result, [{"cancelled": True}])

    def test_worker_errors_do_not_leave_busy_state(self):
        def fail(progress, cancelled):
            raise ValueError("invalid evaluation evidence")
        self.app._guideline_async(fail, lambda _: self.fail("completion must not run"))
        self.pump(lambda: not self.app._guideline_busy)
        self.assertIn("invalid evaluation evidence", self.app.var("guideline_output_text").get())

    def test_official_library_navigation_copy_pdf_and_language(self):
        self.app.navigate("guidelines")
        self.assertEqual(len(self.app.guideline_notebook.tabs()), 4)
        self.assertEqual(len(self.app.guideline_library_tree.get_children()), 9)
        self.app.guideline_library_tree.selection_set("probast_explanation")
        self.app._select_guideline_resource()
        detail = self.app.guideline_library_detail.get("1.0", "end-1c")
        self.assertIn("Europe PMC", detail)
        self.assertIn("직접 참여자 모집", detail)
        self.app.change_language("en")
        self.assertEqual(self.app.var("guideline_resource").get(), "probast_explanation")
        self.assertIn("Official guidelines", self.app.guideline_notebook.tab(3, "text"))
        self.assertIn("No direct participant recruitment", self.app.guideline_library_detail.get("1.0", "end-1c"))
        output = Path(self.temp.name) / "original.pdf"
        with patch("researchbench.guideline_ui.filedialog.asksaveasfilename", return_value=str(output)):
            self.app._save_guideline_original()
        self.pump(lambda: not self.app._guideline_busy)
        self.assertTrue(output.is_file())
        self.assertGreater(len(PdfReader(output).pages), 0)
        with patch("researchbench.guideline_ui.webbrowser.open") as opened:
            self.app._open_guideline_official_page()
            opened.assert_called_once_with("https://www.probast.org/probast_ai/downloads/")

    def test_direct_assessment_pdf_and_application_guide_exports(self):
        self.app.navigate("guidelines")
        self.app._create_guideline_audit(False)
        audit_id = self.app.var("guideline_selected").get()
        before = self.app._guideline_service().get(audit_id)
        output = Path(self.temp.name) / "assessment.pdf"
        with patch("researchbench.guideline_ui.filedialog.asksaveasfilename", return_value=str(output)):
            self.app._export_guideline_pdf()
        self.pump(lambda: not self.app._guideline_busy)
        self.assertTrue(output.is_file())
        self.assertIn("TRIPOD", "".join(p.extract_text() for p in PdfReader(output).pages))
        self.assertEqual(self.app._guideline_service().get(audit_id), before)
        with self.assertRaises(FileExistsError):
            self.app._guideline_service().export_pdf(audit_id, output)
        guide = Path(self.temp.name) / "application-guide.pdf"
        with patch("researchbench.guideline_ui.filedialog.asksaveasfilename", return_value=str(guide)):
            self.app._export_guideline_guide()
        self.pump(lambda: not self.app._guideline_busy)
        text = "".join(p.extract_text() for p in PdfReader(guide).pages)
        for phrase in ("SleepWell", "TRIPOD+AI", "PROBAST+AI", "DECIDE-AI", "V3+", "후향적 외부"):
            self.assertIn(phrase, text)

    def test_report_page_links_to_assessment_and_official_pdf_tabs(self):
        self.app.navigate("reports")
        self.app._open_report_guidelines(1)
        self.assertEqual(self.app.page, "guidelines")
        self.assertEqual(self.app.guideline_notebook.index(self.app.guideline_notebook.select()), 1)
        self.app.navigate("reports")
        self.app._open_report_guidelines(3)
        self.assertEqual(self.app.guideline_notebook.index(self.app.guideline_notebook.select()), 3)


if __name__ == "__main__":
    unittest.main()
