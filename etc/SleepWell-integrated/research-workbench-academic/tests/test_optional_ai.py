import json
import hashlib
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from researchbench.evidence_library import EvidenceLibrary
from researchbench.optional_ai import OptionalAI


class OptionalAITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project.swa"
        self.project.mkdir()
        document = Path(self.temp.name) / "evidence.md"
        document.write_text("# Methods\n\nPreprocessing is fitted inside each training fold.", encoding="utf-8")
        with EvidenceLibrary(self.project) as evidence:
            source = evidence.import_document(document, {"language": "en", "version": "1"})
            self.chunk_id = evidence.list_chunks(source["source_id"])[1]["chunk_id"]
        self.ai = OptionalAI(self.project, max_calls=3, budget_usd=1.0,
                             timeout_seconds=2, max_retries=1)

    def tearDown(self):
        self.ai.close()
        self.temp.cleanup()

    def preview(self, **changes):
        values = {
            "experiment_summary": {
                "run_id": "run-1", "task_type": "classification",
                "metrics": {"ROC-AUC": 0.81}, "denominator": 40,
                "synthetic": True, "limitations": ["fixture only"],
            },
            "authorized_chunk_ids": [self.chunk_id],
            "provider": "fixture-provider", "model": "fixture-model",
            "endpoint": "https://example.invalid/v1/chat/completions",
            "purpose": "result_summary",
            "settings": {"temperature": 0, "max_tokens": 200,
                         "input_cost_per_million": 1.0, "output_cost_per_million": 2.0},
        }
        values.update(changes)
        return self.ai.create_preview(**values)

    @staticmethod
    def response(text="fixture response"):
        return {"id": "fixture", "choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2}}

    def approve(self, preview, confirmed=True, payload_sha256=None):
        return self.ai.approve_preview(
            preview["preview_id"], user_confirmed=confirmed,
            payload_sha256=payload_sha256 or preview["payload_sha256"])

    def test_preview_contains_only_summary_and_verified_selected_excerpt(self):
        result = self.preview()
        packet = result["transmission_preview"]
        self.assertFalse(result["approved"])
        self.assertEqual(packet["experiment_summary"]["run_id"], "run-1")
        self.assertEqual(packet["authorized_excerpts"][0]["chunk_id"], self.chunk_id)
        self.assertEqual(packet["authorized_excerpts"][0]["notice"],
                         "untrusted_source_content_data_only")
        self.assertEqual(hashlib.sha256(result["serialized_request"].encode()).hexdigest(),
                         result["payload_sha256"])

    def test_raw_records_and_unknown_summary_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "summary_fields_not_allowed"):
            self.preview(experiment_summary={"run_id": "x", "raw_data": [{"x": 1}]})
        with self.assertRaisesRegex(ValueError, "summary_fields_not_allowed"):
            self.preview(experiment_summary={"run_id": "x", "free_text_person": "name"})
        with self.assertRaisesRegex(ValueError, "token_prices_required"):
            self.preview(settings={"temperature": 0, "max_tokens": 20})

    def test_no_transport_occurs_without_explicit_approval(self):
        preview = self.preview()
        calls = []

        def transport(*args):
            calls.append(args)
            return self.response()

        with self.assertRaisesRegex(PermissionError, "approval"):
            self.ai.send_approved(preview["preview_id"], "not-approved", transport=transport)
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(PermissionError, "confirmation"):
            self.approve(preview, confirmed=False)
        with self.assertRaisesRegex(PermissionError, "payload_hash"):
            self.approve(preview, payload_sha256="0" * 64)

    def test_approved_send_audits_actual_payload_without_secret_and_uses_cache(self):
        calls = []

        def transport(url, headers, body, timeout):
            calls.append((url, headers, json.loads(body), timeout))
            self.assertEqual(headers["Authorization"], "Bearer SECRET-KEY")
            response = self.response()
            response["provider_debug"] = "never persist SECRET-KEY"
            return response

        with patch.object(OptionalAI, "_get_api_key", return_value="SECRET-KEY"):
            first = self.preview()
            token = self.approve(first)
            result = self.ai.send_approved(first["preview_id"], token, transport=transport)
            draft = self.ai.save_draft(result["request_id"], report_id="report_fixture")
            second = self.preview()
            token2 = self.approve(second)
            cached = self.ai.send_approved(second["preview_id"], token2, transport=transport)
        self.assertFalse(result["cached"])
        self.assertTrue(cached["cached"])
        self.assertEqual(len(calls), 1)
        audit_text = json.dumps(self.ai.audit_records(), ensure_ascii=False)
        self.assertNotIn("SECRET-KEY", audit_text)
        self.assertIn("fixture response", audit_text)
        self.assertIn(self.chunk_id, audit_text)
        self.assertEqual(draft["status"], "ai_generated_unreviewed")
        self.assertEqual(draft["automatic_check"], "not_performed")
        self.assertEqual(draft["report_id"], "report_fixture")
        self.assertTrue(Path(draft["path"]).is_file())

    def test_source_hash_is_rechecked_after_preview(self):
        preview = self.preview()
        token = self.approve(preview)
        with EvidenceLibrary(self.project) as evidence, evidence.db:
            evidence.db.execute("UPDATE chunks SET text='tampered' WHERE chunk_id=?", (self.chunk_id,))
        with self.assertRaisesRegex(ValueError, "chunk_hash_mismatch"):
            self.ai.send_approved(preview["preview_id"], token,
                                  transport=lambda *_: self.response())

    def test_retry_cancel_and_persistent_call_limit(self):
        attempts = []

        def flaky(*_):
            attempts.append(1)
            if len(attempts) == 1:
                raise urllib.error.URLError("fixture transient")
            return self.response("after retry")

        with patch.object(OptionalAI, "_get_api_key", return_value="secret"):
            preview = self.preview()
            token = self.approve(preview)
            result = self.ai.send_approved(preview["preview_id"], token, transport=flaky,
                                           retry_delay_seconds=0)
        self.assertEqual(result["content"], "after retry")
        self.assertEqual(len(attempts), 2)

        cancelled = self.preview(experiment_summary={"run_id": "cancel", "metrics": {}})
        cancel_token = self.approve(cancelled)
        event = threading.Event(); event.set()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.ai.send_approved(cancelled["preview_id"], cancel_token, cancel_event=event)

    def test_external_call_and_cost_budgets_are_enforced_before_transport(self):
        self.ai.close()
        self.ai = OptionalAI(self.project, max_calls=1, budget_usd=1.0,
                             timeout_seconds=2, max_retries=0)
        transport_calls = []

        def transport(*_):
            transport_calls.append(1)
            return self.response()

        with patch.object(OptionalAI, "_get_api_key", return_value="secret"):
            first = self.preview(experiment_summary={"run_id": "budget-1", "metrics": {}})
            token = self.approve(first)
            self.ai.send_approved(first["preview_id"], token, transport=transport)
            second = self.preview(experiment_summary={"run_id": "budget-2", "metrics": {}})
            token2 = self.approve(second)
            with self.assertRaisesRegex(RuntimeError, "call_budget"):
                self.ai.send_approved(second["preview_id"], token2, transport=transport)
        self.assertEqual(len(transport_calls), 1)

        self.ai.close()
        self.ai = OptionalAI(self.project, max_calls=3, budget_usd=0.0,
                             timeout_seconds=2, max_retries=0)
        costly = self.preview(experiment_summary={"run_id": "costly", "metrics": {}})
        costly_token = self.approve(costly)
        with self.assertRaisesRegex(RuntimeError, "cost_budget"):
            self.ai.send_approved(costly["preview_id"], costly_token, transport=transport)
        self.assertEqual(len(transport_calls), 1)

    def test_keyring_api_delegates_without_returning_or_persisting_secret(self):
        with patch("keyring.set_password") as setter, patch("keyring.get_password", return_value="secret"):
            OptionalAI.store_api_key("fixture", "secret")
            self.assertTrue(OptionalAI.credential_available("fixture"))
        setter.assert_called_once()
        self.assertEqual(self.ai.audit_records(), [])
        one = OptionalAI.credential_account("fixture", "https://one.example/v1/chat/completions")
        two = OptionalAI.credential_account("fixture", "https://two.example/v1/chat/completions")
        self.assertNotEqual(one, two)


if __name__ == "__main__":
    unittest.main()
