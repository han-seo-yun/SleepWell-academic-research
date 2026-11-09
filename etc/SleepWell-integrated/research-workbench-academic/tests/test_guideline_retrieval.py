import tempfile
import unittest
from pathlib import Path

from researchbench.evidence_library import EvidenceLibrary
from researchbench.guideline_retrieval import (
    OFFICIAL_RETRIEVAL_DOCUMENTS,
    prepare_guideline_retrieval,
    search_prepared_guidelines,
)


class GuidelineRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "guideline-rag.swa"
        self.project.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_prepares_verified_sources_and_grounded_short_citations(self):
        audit = {
            "audit_id": "audit_fixture",
            "artifact_sha256": "a" * 64,
            "tripod": {"items": [{"status": "satisfied"}, {"status": "not_reported"}]},
            "probast": {
                "questions": [{"answer": "Y"}, {"answer": "PY"}],
                "overall_judgments": [{"domain_id": "overall:evaluation_risk", "judgment": "High"}],
            },
        }
        prepared = prepare_guideline_retrieval(self.project, audit=audit)
        self.assertEqual(prepared["status"], "ready")
        self.assertEqual(prepared["generation"], "deterministic_template_no_llm")
        self.assertFalse(prepared["network_transfer"])
        self.assertFalse(prepared["feasibility"]["full_rag"])
        self.assertEqual(len(prepared["sources"]), len(OFFICIAL_RETRIEVAL_DOCUMENTS))
        self.assertEqual(len(prepared["citations"]), len(OFFICIAL_RETRIEVAL_DOCUMENTS))
        self.assertEqual(prepared["audit_summary"]["tripod_status_counts"]["satisfied"], 1)
        self.assertFalse(prepared["audit_summary"]["indexed"])

        with EvidenceLibrary(self.project) as library:
            for citation in prepared["citations"]:
                chunk = library.get_chunk(citation["chunk_id"])
                search = library.search_log(citation["search_id"])
                self.assertIn(citation["excerpt"], chunk["text"])
                self.assertLessEqual(len(citation["excerpt"].split()), 20)
                self.assertTrue(
                    {"performance", "evaluation", "participants", "predictors", "outcome", "analysis"}
                    .intersection(citation["excerpt"].lower().split())
                )
                self.assertTrue(any(hit["chunk_id"] == citation["chunk_id"] for hit in search["hits"]))
                self.assertTrue(str(chunk["locator"]).startswith("pdf_page:"))

        source_ids = [row["source_id"] for row in prepared["sources"]]
        positive = search_prepared_guidelines(
            self.project,
            "model performance confidence intervals calibration",
            source_ids=source_ids,
        )
        self.assertEqual(positive["status"], "ok")
        unrelated = search_prepared_guidelines(
            self.project,
            "zzqvxx jkjwpp qqqzzz",
            source_ids=source_ids,
        )
        self.assertEqual(unrelated["status"], "no_evidence")

    def test_corrupted_indexed_official_copy_fails_closed(self):
        prepared = prepare_guideline_retrieval(self.project)
        first_source_id = prepared["sources"][0]["source_id"]
        with EvidenceLibrary(self.project) as library:
            source = library.get_source(first_source_id)
            stored = self.project / str(source["stored_path"])
        stored.write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "stored_document_hash_mismatch"):
            prepare_guideline_retrieval(self.project)

    def test_rejects_non_official_source_scope(self):
        note = Path(self.temporary.name) / "note.md"
        note.write_text("model performance", encoding="utf-8")
        with EvidenceLibrary(self.project) as library:
            source = library.import_document(note, {"language": "en"})
        with self.assertRaisesRegex(ValueError, "source_is_not_prepared_official_guideline"):
            search_prepared_guidelines(self.project, "model performance", source_ids=[source["source_id"]])


if __name__ == "__main__":
    unittest.main()
