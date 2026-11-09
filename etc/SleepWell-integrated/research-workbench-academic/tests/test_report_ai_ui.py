import copy
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
from researchbench.ai_connections import load_settings
from researchbench.evidence_library import EvidenceLibrary
from researchbench.report_ai_ui import generate_ai_report
from researchbench.reporting import ReportService
from researchbench.result_adapter import adapt_run
from tests import test_comparison_reporting
from tests.test_simple_reports import SimpleReportHost


class FixtureAgentClient:
    """Deterministic simulated responses, never a substitute for a live API test."""
    def __init__(self):
        self.calls = []

    def complete(self, messages, *, role, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        context = json.loads(messages[1]["content"])
        cid, metric = next(iter(context["metrics"].items()))
        statement = {
            "section": "performance", "text_template": "모의 응답: {{claim:" + cid + "}}. 탐색적 비교이므로 추가 평가가 필요합니다.",
            "evidence_claim_ids": [cid], "evidence_chunk_ids": [], "framework_item_ids": [],
            "model_ids": [metric["model_id"]],
        }
        framework_ids = list(context['framework_items'])[:1]
        chunk_ids = [item['chunk_id'] for item in context.get('retrieved_evidence', [])[:1]]
        statements = [statement,
            dict(statement, section='guidelines', evidence_claim_ids=[], evidence_chunk_ids=chunk_ids,
                 framework_item_ids=framework_ids,
                 text_template='모의 응답: TRIPOD+AI 관련 보고 항목을 근거에 연결했으며, PROBAST+AI 최종 판단에는 평가자 검토가 필요합니다.'),
            dict(statement, section='limitations', framework_item_ids=framework_ids,
                 text_template='모의 응답: 기존 자료에서의 탐색적 비교이며 일반화 판단에는 별도 외부 자료가 필요합니다.')]
        return {"content": json.dumps({"statements": statements, "notes": []}, ensure_ascii=False),
                "provider": "test-fixture", "model": "no-network", "actual_llm": False,
                "usage": {"input_tokens": 10, "output_tokens": 20}}


class ReportAIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = AcademicProject.create(Path(self.temp.name) / "test.swa", "AI report test")
        self.record = test_comparison_reporting.ComparisonReportingTests._record()
        self.record['id'] = str(uuid.uuid4())
        self.record['result']['summary'] = [
            {'model_id': model, 'joint_mae_min': 3.0, 'n_nights': 3, 'n_participants': 2}
            for model in ('a', 'b', 'c')]
        self.project.save_run(self.record)
        self.view = adapt_run(self.record, "sleepwell", self.project.path)
        self.selection = {"run_id": self.view["run_id"], "result_hash": self.view["result_hash"],
                          "model_ids": ["a", "b"], "target": "onset", "averaging": "participant_equal", "tolerance": 2}

    def tearDown(self):
        gc.collect()
        self.temp.cleanup()

    def test_pipeline_pdf_metrics_figures_and_mock_provenance(self):
        client = FixtureAgentClient()
        before = copy.deepcopy(self.selection)
        with patch("researchbench.guideline_retrieval.prepare_guideline_retrieval", return_value={"citations": []}):
            result = generate_ai_report(self.project.path, self.view, self.selection, "비교", "ko",
                                        load_settings(self.project.path), client=client)
        report = result["report"]
        self.assertEqual(report["comparison_evidence"]["selection"], before)
        self.assertEqual(report["automatic_check"]["errors"], [])
        self.assertFalse(report["generation"]["llm_used"])
        self.assertTrue(report["generation"]["test_only"])
        self.assertEqual(len(client.calls), 3)
        destination = Path(self.temp.name) / "mock_report.pdf"
        with EvidenceLibrary(self.project.path) as library:
            service = ReportService(self.project.path, library)
            service.export_pdf(report["report_id"], destination, mode="checked")
        reader = PdfReader(destination)
        text = "\n".join(page.extract_text() for page in reader.pages)
        self.assertIn("모의", text)
        self.assertIn("test-fixture", text)
        self.assertIn("TRIPOD+AI", text)
        self.assertIn("PROBAST+AI", text)
        self.assertNotIn('생성형 LLM·외부 AI 전송은 사용하지 않았다', text)
        self.assertEqual(sum(len(page.images) for page in reader.pages), 7)
        sent = json.dumps(client.calls, ensure_ascii=False)
        self.assertNotIn("participant_id", sent)
        self.assertNotIn(str(self.project.path), sent)

        # Replacing prose and its rendered form cannot bypass the frozen agent trace.
        tampered = copy.deepcopy(report)
        tampered.pop('artifact_sha256', None)
        changed = tampered['ai_analysis']['statements'][0]
        changed['text_template'] += ' 다른 해석입니다.'
        changed['rendered_text'] += ' 다른 해석입니다.'
        with EvidenceLibrary(self.project.path) as library:
            check = ReportService(self.project.path, library).check(tampered)
        self.assertIn('ai_trace_invalid_or_tampered', {error['code'] for error in check['errors']})

    def test_cancel_before_retrieval_does_not_create_report_or_call_provider(self):
        client = FixtureAgentClient()
        event = threading.Event()
        event.set()
        with patch("researchbench.guideline_retrieval.prepare_guideline_retrieval") as retrieve:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                generate_ai_report(self.project.path, self.view, self.selection, "", "ko",
                                   load_settings(self.project.path), client=client, cancel_event=event)
        retrieve.assert_not_called()
        self.assertEqual(client.calls, [])


class ReportAIUITests(unittest.TestCase):
    def setUp(self):
        ReportAIIntegrationTests.setUp(self)
        self.root = tk.Tk()
        self.root.withdraw()
        self.host = SimpleReportHost(self.root, self.project, [self.view])
        self.frame = ttk.Frame(self.root)
        self.frame.pack(fill="both", expand=True)
        with patch("researchbench.report_ai_ui.connections.credential_status", return_value={"available": False}):
            self.host.build_simple_reports(self.frame)

    def tearDown(self):
        self.root.destroy()
        self.host = self.root = None
        ReportAIIntegrationTests.tearDown(self)

    def test_settings_masks_key_saves_to_credential_store_only_and_clears_entry(self):
        with patch("researchbench.report_ai_ui.connections.credential_status", return_value={"available": False}):
            self.host._report_ai_settings()
        self.host.report_ai_settings_window.withdraw()
        self.host.report_ai_settings_vars["model"].set("user-model")
        self.host.report_ai_key_var.set("fixture-secret")
        with patch("researchbench.report_ai_ui.connections.store_key") as store, \
             patch("researchbench.report_ai_ui.connections.credential_status", return_value={"available": True}):
            self.host.report_ai_save_settings_button.invoke()
        self.assertEqual(store.call_args.args[1], "fixture-secret")
        self.assertEqual(self.host.report_ai_key_var.get(), "")
        self.assertNotIn("fixture-secret", (self.project.path / "ai_connection.json").read_text())
        self.assertEqual(load_settings(self.project.path)["model"], "user-model")

    def test_generation_captures_selection_on_main_thread_and_survives_navigation(self):
        settings = {**load_settings(self.project.path), "model": "fixture"}
        captured = {}
        def generate(path, view, selection, purpose, language, supplied, **kwargs):
            captured.update(selection=copy.deepcopy(selection), supplied=supplied)
            kwargs["on_progress"]({"stage": "performance_analyst", "state": "complete"})
            return {"report": {"report_id": "fixture-report"}}
        expected = self.host._simple_report_comparison_selection(self.view)
        with patch("researchbench.report_ai_ui.connections.load_settings", return_value=settings), \
             patch("researchbench.report_ai_ui.connections.credential_status", return_value={"available": True}), \
             patch("researchbench.report_ai_ui.generate_ai_report", side_effect=generate), \
             patch.object(self.host, "_simple_report_refresh_history"):
            self.host._report_ai_generate()
            self.frame.destroy()
            deadline = time.monotonic() + 5
            while getattr(self.host, "_knowledge_worker_active", False) and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
        self.assertFalse(self.host._knowledge_worker_active)
        self.assertEqual(captured["selection"], expected)
        self.assertEqual(self.host.var("knowledge_selected_report").get(), "fixture-report")

    def test_reference_source_disables_ai_but_keeps_keyless_pdf(self):
        self.host.var("knowledge_simple_report_source").set("gradual_v1_reference")
        self.host._simple_report_refresh_comparison()
        self.assertEqual(str(self.host.report_ai_generate_button.cget("state")), "disabled")
        self.assertEqual(str(self.host.simple_report_create_button.cget("state")), "normal")


if __name__ == "__main__":
    unittest.main()
