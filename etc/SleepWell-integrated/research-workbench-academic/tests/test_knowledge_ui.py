import gc
import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
import uuid
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

from pypdf import PdfReader

from researchbench.academic_store import AcademicProject
from researchbench.evidence_library import EvidenceLibrary
from researchbench.knowledge_ui import KnowledgeUIMixin
from researchbench.result_adapter import adapt_run


class KnowledgeHost(KnowledgeUIMixin):
    def __init__(self, root, project, view, lang="ko"):
        self.root = root
        self.project = project
        self.lang = lang
        self.vars = {}
        self.status = tk.StringVar(root, value="")
        self.view = view

    def var(self, key, default=""):
        if key not in self.vars:
            self.vars[key] = tk.StringVar(self.root, value=default)
        return self.vars[key]

    def safe(self, function):
        return function()

    def t(self, key):
        return key

    def platform_runs(self):
        return [self.view]

    def navigate(self, page):
        self.page = page


class KnowledgeUITests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk unavailable: {error}")
        self.root.withdraw()
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.project = AcademicProject.create(self.workspace / "knowledge.swa", "Knowledge UI")
        run_id = str(uuid.uuid4())
        self.record = {
            "id": run_id,
            "name": "Retrospective fixture",
            "status": "completed",
            "config": {"mode": "retrospective", "data_hash": "data-v1"},
            "created": "2026-01-01T00:00:00+00:00",
            "duration": 1.0,
            "result": {
                "mode": "retrospective",
                "summary": [
                    {"model_id": "cmi.data", "joint_mae_min": 12.5, "n_nights": 2, "n_participants": 1}
                ],
                "raw": {
                    "predictions": [
                        {
                            "model_id": "cmi.data", "participant_id": "p1", "cutoff": "2026-01-01",
                            "onset_offset_min": 5.0, "wake_offset_min": 8.0,
                            "actual_onset_offset_min": 4.0, "actual_wake_offset_min": 9.0,
                        }
                    ]
                },
                "provenance": {"executed": True},
            },
        }
        self.project.save_run(self.record)
        self.view = adapt_run(self.record, "sleepwell", self.project.path)
        self.host = KnowledgeHost(self.root, self.project, self.view, "ko")
        self.host.var("knowledge_report_template").set("research")

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.host = None
        self.root = None
        gc.collect()
        self.temp.cleanup()

    def frame(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True)
        return frame

    def register_evidence(self):
        path = self.workspace / "method.md"
        path.write_text("# 방법\n\n결측치 처리는 각 학습 분할 안에서 적합한다.", encoding="utf-8")
        source = self.host._evidence_import_sync(
            path,
            {"title": "누수 방지", "authors": ["Research Group"], "version": "1", "language": "ko"},
        )
        result = self.host._evidence_search_sync("결측치 학습 분할", {"lang": "ko", "top_k": 5})
        self.assertEqual(result["status"], "ok")
        citation = dict(result["hits"][0])
        citation["search_id"] = result["search_id"]
        return source, result, citation

    def test_evidence_page_uses_bilingual_resources_and_persists_attached_search_hit(self):
        frame = self.frame()
        self.host.build_evidence(frame)
        self.root.update()
        tabs = [self.host.knowledge_evidence_notebook.tab(index, "text") for index in range(3)]
        self.assertEqual(tabs, ["문서 라이브러리", "근거 검색", "원본 모델 근거"])
        source, result, _citation = self.register_evidence()
        self.host._refresh_sources()
        self.assertTrue(self.host.knowledge_source_tree.exists(source["source_id"]))
        self.host._populate_search_results(result)
        chunk_id = result["hits"][0]["chunk_id"]
        self.host.knowledge_search_tree.selection_set(chunk_id)
        self.host._attach_selected_citation()
        self.host.var("knowledge_query").set("결측치 학습 분할")
        self.host.capture_knowledge_drafts()
        self.project.save()

        reopened = AcademicProject.open(self.project.path)
        english = KnowledgeHost(self.root, reopened, self.view, "en")
        english_frame = self.frame()
        english.build_evidence(english_frame)
        self.root.update()
        english_tabs = [english.knowledge_evidence_notebook.tab(index, "text") for index in range(3)]
        self.assertEqual(english_tabs, ["Document Library", "Evidence Search", "Original Model Provenance"])
        self.assertEqual(english.var("knowledge_query").get(), "결측치 학습 분할")
        self.assertEqual(english.knowledge_citations[0]["chunk_id"], chunk_id)
        self.assertEqual(english.knowledge_citations[0]["search_id"], result["search_id"])
        self.assertFalse(hasattr(english, "knowledge_provenance_json"))
        self.assertTrue(
            any(row.get("family") == "external_validation_plans" for row in english.knowledge_provenance_payload["catalog"]["coverage_matrix"])
        )
        self.assertTrue(english.knowledge_provenance_runs.exists(self.view["run_id"]))
        english.knowledge_provenance_runs.selection_set(self.view["run_id"])
        english._provenance_run_selected()
        detail_values = [english.knowledge_provenance_detail.item(item, "values") for item in english.knowledge_provenance_detail.get_children()]
        self.assertTrue(any(values[0] == "Result Hash" and values[1] == self.view["result_hash"] for values in detail_values))
        self.assertTrue(any(values[0] == "Hash Verification" and values[1] == "Verified" for values in detail_values))

    def test_report_page_creates_edits_checks_reviews_and_translates_real_versions(self):
        _source, _result, citation = self.register_evidence()
        report = self.host._create_report_sync(
            [self.view], "research", "전처리는 누수 없이 평가되었는가?", [citation]
        )
        frame = self.frame()
        self.host.build_reports(frame)
        self.host._refresh_report_versions(report["report_id"])
        self.root.update()
        self.assertTrue(self.host.knowledge_report_versions.exists(report["report_id"]))
        claim = next(item for item in report["claims"] if item["claim_type"] == "experiment_fact")
        edited = self.host._save_report_edit_sync(
            report["report_id"], f"claim:{claim['claim_id']}", "검토 대상: " + claim["text"]
        )
        self.assertNotEqual(edited["report_id"], report["report_id"])
        checked = self.host._check_report_sync(edited["report_id"])
        self.assertEqual(checked["automatic_check"]["state"], "automatic_check_passed")
        reviewed = self.host._record_review_sync(checked["report_id"], "Reviewer A", "원 근거 위치 확인")
        self.assertEqual(reviewed["status"], "user_review_recorded")
        self.assertFalse(reviewed["human_reviews"][-1]["certification"])
        translated = self.host._translate_report_sync(edited["report_id"], "en")
        self.assertEqual(translated["language"], "en")
        self.assertEqual(
            [item["evidence"] for item in translated["claims"]],
            [item["evidence"] for item in edited["claims"]],
        )
        original_reopened = self.host._get_report_sync(report["report_id"])
        self.assertEqual(original_reopened["language"], "ko")
        self.assertEqual(len(self.host._list_reports_sync()), 5)

        english = KnowledgeHost(self.root, self.project, self.view, "en")
        english.var("knowledge_report_template").set("research")
        english_frame = self.frame()
        english.build_reports(english_frame)
        self.root.update()
        self.assertEqual(english.knowledge_template_combo.get(), "Research Report")
        self.assertIn("Report ID", english.knowledge_report_versions.heading("report_id", "text"))

    def test_unsaved_editor_text_survives_language_rebuild_and_checklist_is_user_set(self):
        _source, _result, citation = self.register_evidence()
        report = self.host._create_report_sync([self.view], "model_card", "모델 카드", [citation])
        frame = self.frame()
        self.host.build_reports(frame)
        self.host._refresh_report_versions(report["report_id"])
        claim = next(item for item in report["claims"] if item["claim_type"] == "experiment_fact")
        node = f"claim:{claim['claim_id']}"
        self.host.knowledge_claim_tree.selection_set(node)
        self.host._report_node_selected()
        draft = "저장 전 사용자 문구: " + claim["text"]
        self.host.knowledge_report_editor.delete("1.0", "end")
        self.host.knowledge_report_editor.insert("1.0", draft)
        self.host.capture_knowledge_drafts()
        frame.destroy()

        self.host.lang = "en"
        rebuilt = self.frame()
        self.host.build_reports(rebuilt)
        self.host._refresh_report_versions(report["report_id"])
        self.root.update()
        self.assertEqual(self.host.knowledge_report_editor.get("1.0", "end-1c"), draft)

        checklist_node = "checklist:card_details"
        self.host.knowledge_claim_tree.selection_set(checklist_node)
        self.host._report_node_selected()
        self.host.knowledge_checklist_combo.current(0)
        updated = self.host._save_report_edit_sync(
            report["report_id"], checklist_node, "Reviewer explicitly assessed this item."
        )
        card_item = next(
            item
            for framework in updated["checklists"]
            for item in framework["items"]
            if item["item_id"] == "card_details"
        )
        self.assertEqual(card_item["status"], "satisfied")
        self.assertEqual(card_item["assessed_by"], "user")

    def test_worker_keeps_tk_event_loop_available(self):
        release = threading.Event()
        completed = []
        self.host._knowledge_async(lambda: release.wait(2) or "done", lambda value: completed.append(value))
        self.assertTrue(self.host._knowledge_worker_active)
        marker = tk.StringVar(self.root, value="pending")
        self.root.after(1, lambda: marker.set("responsive"))
        deadline = time.time() + 0.5
        while time.time() < deadline and marker.get() != "responsive":
            self.root.update()
            time.sleep(0.005)
        self.assertEqual(marker.get(), "responsive")
        release.set()
        deadline = time.time() + 2
        while time.time() < deadline and not completed:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(completed, [True])

    def test_create_report_button_generates_pdf_and_opens_matching_version(self):
        self.host.build_reports(self.frame())
        self.host.knowledge_run_tree.selection_set(self.view["run_id"])
        self.assertIn("PDF", self.host.knowledge_create_pdf_button.cget("text"))
        destination = self.workspace / "created-report.pdf"
        with patch("researchbench.knowledge_ui.filedialog.asksaveasfilename", return_value=str(destination)):
            self.host.knowledge_create_pdf_button.invoke()
        deadline = time.monotonic() + 20
        while getattr(self.host, "_knowledge_worker_active", False) and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.host._knowledge_worker_active)
        self.assertTrue(destination.is_file(), self.host.status.get())
        self.assertIn("12.5", "".join(p.extract_text() for p in PdfReader(destination).pages))
        self.assertEqual(len(self.host._list_reports_sync()), 1)
        self.assertEqual(self.host.var("knowledge_pdf_report_id").get(), self.host.var("knowledge_selected_report").get())
        with patch("researchbench.knowledge_ui.os.startfile", create=True) as opened:
            self.host.knowledge_open_pdf_button.invoke()
            opened.assert_called_once_with(str(destination))
        other = self.host._create_report_sync([self.view], "research", "Other version", [])
        self.host._refresh_report_versions(other["report_id"])
        self.assertIn("disabled", self.host.knowledge_open_pdf_button.state())
        with self.assertRaises(ValueError):
            self.host._open_report_pdf()

    def test_cancel_create_pdf_does_not_create_a_document(self):
        self.host.build_reports(self.frame())
        self.host.knowledge_run_tree.selection_set(self.view["run_id"])
        with patch("researchbench.knowledge_ui.filedialog.asksaveasfilename", return_value=""):
            self.host.knowledge_create_pdf_button.invoke()
        self.assertFalse(getattr(self.host, "_knowledge_worker_active", False))
        self.assertEqual(self.host._list_reports_sync(), [])

    def test_gradual_primary_button_needs_no_selected_run_and_fills_pdf(self):
        self.host.build_reports(self.frame())
        self.host.var("knowledge_guideline_retrieval").set("0")
        self.assertFalse(self.host.knowledge_run_tree.selection())
        destination = self.workspace / "gradual-report.pdf"
        with patch("researchbench.knowledge_ui.filedialog.asksaveasfilename", return_value=str(destination)):
            self.host.knowledge_gradual_pdf_button.invoke()
        deadline = time.monotonic() + 60
        while getattr(self.host, "_knowledge_worker_active", False) and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.host._knowledge_worker_active)
        self.assertTrue(destination.is_file(), self.host.status.get())
        report = self.host._get_report_sync(self.host.var("knowledge_selected_report").get())
        self.assertEqual(report["template"], "gradual_guidelines")
        self.assertFalse(report["run_refs"])
        self.assertEqual(sum(len(framework["items"]) for framework in report["checklists"]), 86)
        text = "".join(page.extract_text() for page in PdfReader(destination).pages)
        self.assertIn("47.49", text)
        self.assertIn("68.11", text)
        self.assertIn("TRIPOD", text)
        self.assertIn("PROBAST", text)
        self.assertIn("점검 근거 문서 색인", text)
        self.assertIn("robust-v2 학습·선택 프로토콜", text)
        self.assertNotIn("제공되지 않음", text)

    def test_gradual_report_uses_local_guideline_retrieval(self):
        _source, _result, citation = self.register_evidence()
        with patch("researchbench.guideline_retrieval.prepare_guideline_retrieval",
                   return_value={"citations": [citation]}) as retrieve:
            report = self.host._create_report_sync([], "gradual_guidelines", "", [])
        retrieve.assert_called_once_with(self.project.path)
        self.assertTrue(any(claim["evidence"]["kind"] == "literature" for claim in report["claims"]))

    def test_new_report_workspace_defaults_to_gradual_template(self):
        self.host.var("knowledge_report_template").set("")
        self.host.build_reports(self.frame())
        self.assertEqual(self.host.var("knowledge_report_template").get(), "gradual_guidelines")

    def test_pdf_completion_survives_navigation_away_from_report_widgets(self):
        frame = self.frame()
        self.host.build_reports(frame)
        frame.destroy()
        path = self.workspace / "exported.pdf"
        path.write_bytes(b"%PDF-fixture")
        self.host._after_report_pdf_export("report-example", path)
        self.assertEqual(self.host.var("knowledge_pdf_path").get(), str(path))


if __name__ == "__main__":
    unittest.main()
