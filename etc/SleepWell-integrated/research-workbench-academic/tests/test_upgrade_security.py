import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from researchbench.evidence_library import EvidenceLibrary
from researchbench.optional_ai import OptionalAI, _SameOriginRedirectHandler


class OptionalAISecurityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "security.swa"
        self.project.mkdir()
        source_file = Path(self.temp.name) / "source.md"
        source_file.write_text("# Evidence\n\nOnly verified aggregate evidence is authorized.", encoding="utf-8")
        with EvidenceLibrary(self.project) as library:
            source = library.import_document(source_file, {"language": "en"})
            self.chunk_id = library.list_chunks(source["source_id"])[1]["chunk_id"]
        self.ai = OptionalAI(self.project, max_calls=5, budget_usd=5,
                             timeout_seconds=2, max_retries=0)

    def tearDown(self):
        self.ai.close()
        self.temp.cleanup()

    def preview(self, endpoint: str, run_id: str = "run-security"):
        return self.ai.create_preview(
            experiment_summary={"run_id": run_id, "metrics": {"rmse": 1.2}},
            authorized_chunk_ids=[self.chunk_id], provider="fixture-provider",
            model="fixture-model", endpoint=endpoint, purpose="result_summary",
            settings={"max_tokens": 40, "temperature": 0,
                      "input_cost_per_million": 1.0, "output_cost_per_million": 1.0},
        )

    def approve(self, preview):
        return self.ai.approve_preview(preview["preview_id"], user_confirmed=True,
                                       payload_sha256=preview["payload_sha256"])

    @staticmethod
    def response(content: str):
        return {"choices": [{"message": {"content": content}}]}

    def test_endpoint_is_in_approved_hash_cache_key_and_request_audit(self):
        calls = []

        def transport(url, *_):
            calls.append(url)
            return self.response(url)

        first = self.preview("https://one.example/v1/chat/completions")
        second = self.preview("https://two.example/v1/chat/completions")
        self.assertNotEqual(first["payload_sha256"], second["payload_sha256"])
        with patch.object(OptionalAI, "_get_api_key", return_value="secret"):
            one = self.ai.send_approved(first["preview_id"], self.approve(first), transport=transport)
            two = self.ai.send_approved(second["preview_id"], self.approve(second), transport=transport)
        self.assertFalse(one["cached"])
        self.assertFalse(two["cached"])
        self.assertEqual(calls, ["https://one.example/v1/chat/completions",
                                 "https://two.example/v1/chat/completions"])
        records = self.ai.audit_records()
        self.assertEqual([item["endpoint"] for item in records], calls)
        self.assertNotEqual(records[0]["cache_key"], records[1]["cache_key"])

    def test_sending_record_and_cost_reservation_exist_before_transport(self):
        preview = self.preview("https://audit.example/v1/chat/completions")
        observed = {}

        def transport(*_):
            connection = sqlite3.connect(self.ai.db_path)
            row = connection.execute(
                "SELECT status,attempts,endpoint,charged_cost_usd,owner_pid FROM requests"
            ).fetchone()
            connection.close()
            observed.update(status=row[0], attempts=row[1], endpoint=row[2],
                            charged=row[3], owner_pid=row[4])
            return self.response("ok")

        with patch.object(OptionalAI, "_get_api_key", return_value="secret"):
            self.ai.send_approved(preview["preview_id"], self.approve(preview), transport=transport)
        self.assertEqual(observed["status"], "sending")
        self.assertEqual(observed["attempts"], 1)
        self.assertEqual(observed["endpoint"], "https://audit.example/v1/chat/completions")
        self.assertGreater(observed["charged"], 0)
        self.assertEqual(observed["owner_pid"], os.getpid())

    def test_inflight_reservation_blocks_concurrent_service_budget(self):
        first = self.preview("https://budget.example/v1/chat/completions", "first")
        blocked = []

        def transport(*_):
            with OptionalAI(self.project, max_calls=1, budget_usd=5,
                            timeout_seconds=2, max_retries=0) as second:
                candidate = second.create_preview(
                    experiment_summary={"run_id": "second", "metrics": {}},
                    authorized_chunk_ids=[self.chunk_id], provider="fixture-provider",
                    model="fixture-model", endpoint="https://budget.example/v1/chat/completions",
                    purpose="result_summary",
                    settings={"max_tokens": 40, "temperature": 0,
                              "input_cost_per_million": 1.0, "output_cost_per_million": 1.0},
                )
                token = second.approve_preview(candidate["preview_id"], user_confirmed=True,
                                               payload_sha256=candidate["payload_sha256"])
                with self.assertRaisesRegex(RuntimeError, "call_budget"):
                    second.send_approved(candidate["preview_id"], token,
                                         transport=lambda *_args: self.response("must not run"))
                blocked.append(True)
            return self.response("first complete")

        with patch.object(OptionalAI, "_get_api_key", return_value="secret"):
            self.ai.send_approved(first["preview_id"], self.approve(first), transport=transport)
        self.assertEqual(blocked, [True])

    def test_stale_sending_record_recovers_as_unknown_outcome(self):
        preview = self.preview("https://recovery.example/v1/chat/completions")
        with patch.object(OptionalAI, "_get_api_key", return_value="secret"):
            result = self.ai.send_approved(preview["preview_id"], self.approve(preview),
                                           transport=lambda *_: self.response("done"))
        with self.ai.db:
            self.ai.db.execute(
                """UPDATE requests SET status='sending',completed_at=NULL,owner_pid=0
                     WHERE request_id=?""", (result["request_id"],))
        self.ai.close()
        self.ai = OptionalAI(self.project)
        record = self.ai.audit_records()[0]
        self.assertEqual(record["status"], "interrupted_unknown_outcome")
        self.assertEqual(record["error"], "process_ended_while_external_outcome_unknown")

    def test_cancel_after_uncertain_attempt_is_not_mislabeled_as_safe_cancellation(self):
        preview = self.preview("https://uncertain.example/v1/chat/completions")
        event = threading.Event()

        def uncertain(*_):
            event.set()
            raise urllib.error.URLError("connection lost after send")

        self.ai.max_retries = 1
        with patch.object(OptionalAI, "_get_api_key", return_value="secret"):
            with self.assertRaises(RuntimeError):
                self.ai.send_approved(preview["preview_id"], self.approve(preview),
                                      cancel_event=event, transport=uncertain,
                                      retry_delay_seconds=0)
        record = self.ai.audit_records()[0]
        self.assertEqual(record["status"], "interrupted_unknown_outcome")
        self.assertEqual(record["attempts"], 1)
        self.assertGreater(record["charged_cost_usd"], 0)

    def test_cross_origin_redirect_and_insecure_endpoint_are_blocked(self):
        handler = _SameOriginRedirectHandler()
        request = urllib.request.Request(
            "https://safe.example/v1/chat/completions", data=b"{}",
            headers={"Authorization": "Bearer secret"}, method="POST")
        with self.assertRaisesRegex(ValueError, "cross_origin_redirect"):
            handler.redirect_request(request, None, 307, "redirect", {},
                                     "https://attacker.example/steal")
        with self.assertRaisesRegex(ValueError, "requires_https"):
            OptionalAI._validate_endpoint("http://attacker.example/v1")
        with self.assertRaisesRegex(ValueError, "must_not_contain_credentials"):
            OptionalAI._validate_endpoint("https://safe.example/v1?api_key=SECRET")
        self.assertEqual(OptionalAI._validate_endpoint("http://127.0.0.1:8000/v1"),
                         "http://127.0.0.1:8000/v1")

    def test_cache_and_request_response_tampering_are_detected(self):
        endpoint = "https://integrity.example/v1/chat/completions"
        first = self.preview(endpoint)
        second = self.preview(endpoint)
        with patch.object(OptionalAI, "_get_api_key", return_value="secret"):
            result = self.ai.send_approved(first["preview_id"], self.approve(first),
                                           transport=lambda *_: self.response("original"))
        with self.ai.db:
            self.ai.db.execute("UPDATE cache SET response_json=?",
                               ('{"choices":[{"message":{"content":"forged"}}]}',))
        with self.assertRaisesRegex(ValueError, "cache_response_hash_mismatch"):
            self.ai.send_approved(second["preview_id"], self.approve(second),
                                  transport=lambda *_: self.response("must not run"))

        with self.ai.db:
            self.ai.db.execute("UPDATE requests SET response_json=? WHERE request_id=?",
                               ('{"choices":[{"message":{"content":"forged audit"}}]}',
                                result["request_id"]))
        with self.assertRaisesRegex(ValueError, "request_response_hash_mismatch"):
            self.ai.audit_records()


if __name__ == "__main__":
    unittest.main()
