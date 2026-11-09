import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from researchbench.ai_connections import ProviderClient
from researchbench.evidence_library import EvidenceLibrary
from researchbench.report_agents import (AgentRunCancelled, ReportAgentHarness,
                                         render_ai_sections, validate_ai_analysis)
from researchbench.reporting import ReportService
from researchbench.result_adapter import adapt_run


class FakeClient:
    def __init__(self, outputs, *, actual=False, cost=0.0):
        self.outputs = list(outputs)
        self.actual = actual
        self.cost = cost
        self.calls = []

    def complete(self, messages, *, role, max_output_tokens=None, timeout_seconds=None):
        self.calls.append((role, copy.deepcopy(messages), max_output_tokens, timeout_seconds))
        content = self.outputs.pop(0)
        return {"content": json.dumps(content), "provider": "fixture", "model": "fixture-model",
                "usage": {"total_tokens": 10, "estimated_cost_usd": self.cost},
                "request_id": f"request-{len(self.calls)}", "actual_llm": self.actual}


class ReportAgentHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "study.swa"
        (self.project / "runs").mkdir(parents=True)
        record = self._record()
        (self.project / "runs" / "run.json").write_text(json.dumps(record), encoding="utf-8")
        self.view = adapt_run(record, "sleepwell", self.project)
        self.library = EvidenceLibrary(self.project)
        self.service = ReportService(self.project, self.library)
        self.report = self.service.create_comparison(self.view, {
            "run_id": self.view["run_id"], "result_hash": self.view["result_hash"],
            "model_ids": ["a", "b"], "target": "onset", "averaging": "participant_equal", "tolerance": 2,
        }, lang="ko")
        self.metric_ids = [c["claim_id"] for c in self.report["claims"]
                           if c["evidence"]["kind"] == "comparison_metric"]

    def tearDown(self):
        self.library.close()
        self.temp.cleanup()

    @staticmethod
    def _record():
        predictions = []
        for model, values in (("a", [0, 0, 10]), ("b", [3, 3, 3]), ("c", [-3, -3, -3])):
            for index, (participant, cutoff) in enumerate((("p1", "n1"), ("p1", "n2"), ("p2", "n1"))):
                predictions.append({"participant_id": participant, "cutoff": cutoff, "model_id": model,
                                    "actual_onset_offset_min": 0, "onset_offset_min": values[index],
                                    "actual_wake_offset_min": 480, "wake_offset_min": 480,
                                    "actual_interval_min": 480, "interval_min": 480})
        return {"id": "run", "name": "fixture", "status": "completed",
                "config": {"mode": "report", "data_hash": "data", "input_synthetic": False,
                           "split": "reused_test", "participants": 2},
                "result": {"mode": "report", "summary": [], "raw": {"predictions": predictions},
                           "provenance": {"executed": True, "versions": {"fixture": "1"}}}}

    def outputs(self, statement=None):
        statement = statement or {
            "section": "performance", "text_template": f"점추정치는 {self._token(self.metric_ids[0])}로 보고되며 사람 검토가 필요하다.",
            "evidence_claim_ids": [self.metric_ids[0]], "evidence_chunk_ids": [],
            "framework_item_ids": ["tripod_ai_12e"], "model_ids": ["a"],
        }
        guideline = {
            "section": "guidelines", "text_template": "TRIPOD+AI와 PROBAST+AI 항목은 평가자 검토가 필요하다.",
            "evidence_claim_ids": [], "evidence_chunk_ids": [],
            "framework_item_ids": ["tripod_ai_12e"], "model_ids": [],
        }
        limitation = {
            "section": "limitations", "text_template": "현재 비교는 점추정치이며 외부 자료 검증과 신뢰구간을 산출하지 않았습니다.",
            "evidence_claim_ids": [], "evidence_chunk_ids": [],
            "framework_item_ids": ["probast_ai_analysis"], "model_ids": [],
        }
        return [{"statements": [], "notes": ["aggregate only"]},
                {"statements": [], "notes": ["assessor review"]},
                {"statements": [statement, guideline, limitation], "notes": []}]

    @staticmethod
    def _token(claim_id):
        return "{{claim:" + claim_id + "}}"

    def run_analysis(self, client=None, **settings):
        client = client or FakeClient(self.outputs())
        return ReportAgentHarness(self.project, client, settings=settings).run(self.report), client

    def derived(self, analysis):
        report = copy.deepcopy(self.report)
        report["ai_analysis"] = analysis
        report["generation"] = {"mode": "llm_rag_agents", "llm_used": analysis["actual_llm"],
                                "provider": analysis["provider"], "model": analysis["model"],
                                "test_only": analysis["test_only"], "prompt_version": "report-agents-v1"}
        return report

    def test_selected_subset_binding_and_base_report_are_immutable(self):
        before = copy.deepcopy(self.report)
        analysis, client = self.run_analysis()
        self.assertEqual(self.report, before)
        self.assertEqual(analysis["base_binding"]["model_ids"], ["a", "b"])
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(all(call[2] == 1600 and 0 < call[3] <= 60 for call in client.calls))
        self.assertEqual(validate_ai_analysis(self.service, self.derived(analysis)), [])
        sections = render_ai_sections(self.derived(analysis))
        self.assertEqual(len(sections), 4)
        self.assertIn("모의·시험", sections[0]["paragraphs"][0])
        self.assertIn("a", sections[1]["paragraphs"][0])
        self.assertNotIn("{{claim:", sections[1]["paragraphs"][0])
        serialized_prompts = json.dumps(client.calls, ensure_ascii=False)
        self.assertNotIn("participant_id", serialized_prompts)
        self.assertNotIn(str(self.project), serialized_prompts)

    def test_unbound_number_fabricated_ci_unknown_evidence_and_model_are_blocked(self):
        bad = copy.deepcopy(self.outputs()[-1]["statements"][0])
        cases = [
            {**bad, "section": "MAE 99.9"},
            {**bad, "text_template": "MAE 9.9로 가장 우수하다."},
            {**bad, "text_template": "신뢰구간이 확인되었다."},
            {**bad, "evidence_claim_ids": ["forged"]},
            {**bad, "model_ids": ["unknown"]},
        ]
        for statement in cases:
            with self.subTest(statement=statement):
                client = FakeClient(self.outputs(statement))
                with self.assertRaises(ValueError):
                    ReportAgentHarness(self.project, client, settings={"max_calls": 3}).run(self.report)

    def test_optional_tool_is_allowlisted_and_injection_is_only_untrusted_text(self):
        client = FakeClient([
            {"tool": {"name": "read_metrics", "arguments": {}}},
            {"statements": [], "notes": []},
            {"statements": [], "notes": []},
            self.outputs()[-1],
        ])
        analysis = ReportAgentHarness(self.project, client).run(self.report)
        trace = json.loads((self.project / analysis["trace_relative_path"]).read_text(encoding="utf-8"))
        self.assertEqual([e["name"] for e in trace["events"] if e["event"] == "tool"], ["read_metrics"])
        malicious = FakeClient([{"tool": {"name": "filesystem", "arguments": {"path": "secrets"}}}])
        with self.assertRaisesRegex(ValueError, "unsupported_agent_tool"):
            ReportAgentHarness(self.project, malicious).run(self.report)

    def test_cancel_call_and_cost_caps_persist_no_successful_analysis(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(AgentRunCancelled):
            ReportAgentHarness(self.project, FakeClient(self.outputs()), cancel_event=event).run(self.report)
        traces = list((self.project / "research" / "agent_runs").glob("*.json"))
        self.assertTrue(any(json.loads(p.read_text())["status"] == "cancelled" for p in traces))
        with self.assertRaisesRegex(RuntimeError, "call_limit"):
            self._run_call_limit()
        with self.assertRaisesRegex(RuntimeError, "cost_limit"):
            ReportAgentHarness(self.project, FakeClient(self.outputs(), cost=0.2),
                               settings={"max_cost_usd": 0.1}).run(self.report)

    def _run_call_limit(self):
        client = FakeClient([{"tool": {"name": "read_metrics", "arguments": {}}}] * 4)
        return ReportAgentHarness(self.project, client, settings={"max_calls": 3, "max_tool_calls": 4}).run(self.report)

    def test_mock_and_real_labels_are_explicit(self):
        mock, _ = self.run_analysis(FakeClient(self.outputs(), actual=False))
        real, _ = self.run_analysis(FakeClient(self.outputs(), actual=True))
        self.assertEqual((mock["actual_llm"], mock["test_only"]), (False, True))
        self.assertEqual((real["actual_llm"], real["test_only"]), (True, False))

    def test_source_revalidation_base_mutation_and_trace_tamper_are_blocked(self):
        analysis, _ = self.run_analysis()
        derived = self.derived(analysis)
        mutated = copy.deepcopy(derived)
        mutated["claims"][0]["text"] = "forged"
        self.assertIn("ai_analysis_base_binding_mismatch", {e["code"] for e in validate_ai_analysis(self.service, mutated)})
        statement_tamper = copy.deepcopy(derived)
        statement_tamper["ai_analysis"]["statements"][0]["rendered_text"] = "forged"
        self.assertIn("ai_statement_render_mismatch", {e["code"] for e in validate_ai_analysis(self.service, statement_tamper)})
        source_tamper = copy.deepcopy(derived)
        source_tamper["ai_analysis"]["retrieved_evidence"].append(
            {"chunk_id": "forged", "source_id": "forged", "text_sha256": "0" * 64,
             "title": "", "locator": "", "excerpt": "", "base_claim_id": None})
        self.assertIn("ai_evidence_revalidation_failed",
                      {e["code"] for e in validate_ai_analysis(self.service, source_tamper)})
        trace_path = self.project / analysis["trace_relative_path"]
        trace_path.write_text(trace_path.read_text() + " ", encoding="utf-8")
        self.assertIn("ai_trace_invalid_or_tampered", {e["code"] for e in validate_ai_analysis(self.service, derived)})

    def test_provider_cannot_change_identity_mid_run(self):
        client = FakeClient(self.outputs())
        original = client.complete
        def changing(messages, **kwargs):
            result = original(messages, **kwargs)
            if len(client.calls) == 2:
                result["model"] = "other-model"
            return result
        client.complete = changing
        with self.assertRaisesRegex(ValueError, "provider_identity_changed"):
            ReportAgentHarness(self.project, client).run(self.report)

    def test_writer_gets_one_bounded_schema_repair(self):
        bad = copy.deepcopy(self.outputs()[-1])
        bad["statements"][0]["text_template"] = "unsupported 99"
        client = FakeClient(self.outputs()[:2] + [bad, self.outputs()[-1]])
        analysis = ReportAgentHarness(self.project, client).run(self.report)
        trace = json.loads((self.project / analysis["trace_relative_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len([event for event in trace["events"] if event["event"] == "bounded_repair"]), 1)

    def test_report_service_attaches_immutable_checked_descendant(self):
        analysis, _ = self.run_analysis()
        source_path = self.service._report_path(self.report["report_id"])
        before = source_path.read_bytes()
        derived = self.service.attach_ai_analysis(self.report["report_id"], analysis)
        self.assertEqual(source_path.read_bytes(), before)
        self.assertEqual(derived["parent_report_id"], self.report["report_id"])
        self.assertEqual(derived["generation"]["mode"], "llm_rag_agents")
        self.assertEqual(self.service.check(derived)["errors"], [])
        tampered = copy.deepcopy(derived)
        tampered["generation"]["provider"] = "forged"
        self.assertIn("ai_generation_provider_mismatch",
                      {item["code"] for item in self.service.check(tampered)["errors"]})
        with self.assertRaisesRegex(ValueError, "translation_requires_regeneration"):
            self.service.translate(derived["report_id"], "en")

    def test_real_provider_transport_contract_is_interoperable(self):
        outputs = [json.dumps(item, ensure_ascii=False) for item in self.outputs()]
        def transport(_method, _url, _headers, payload, _timeout):
            self.assertTrue(all(message["role"] in {"system", "user", "assistant"}
                                for message in payload["messages"]))
            return {"id": "transport-fixture", "choices": [{"message": {"content": outputs.pop(0)}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
        client = ProviderClient({"schema": 1, "provider": "openai", "endpoint": "https://api.openai.com/v1",
                                 "model": "gpt-fixture", "max_calls": 6, "max_output_tokens": 2000,
                                 "timeout_seconds": 30, "total_timeout_seconds": 120}, transport=transport)
        analysis = ReportAgentHarness(self.project, client).run(self.report)
        self.assertFalse(analysis["actual_llm"])
        self.assertTrue(analysis["test_only"])


if __name__ == "__main__":
    unittest.main()
