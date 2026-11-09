import json
import importlib.util
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchbench.evidence_library import EvidenceLibrary, run_evaluation


ROOT = Path(__file__).resolve().parents[1]


class EvidenceLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "one.swa"
        self.project.mkdir()
        self.library = EvidenceLibrary(self.project)

    def tearDown(self):
        self.library.close()
        self.temp.cleanup()

    def document(self, name="note.md", text="# Methods\n\nFit preprocessing inside training folds."):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_import_preserves_copy_hash_and_location(self):
        source = self.library.import_document(self.document(), {"language":"en", "version":"1"})
        self.assertEqual(source["extraction_status"], "extracted")
        chunks = self.library.list_chunks(source["source_id"])
        self.assertEqual(chunks[0]["locator_type"], "paragraph")
        self.assertTrue(chunks[0]["locator"].startswith("paragraph:"))
        self.assertEqual(len(source["stored_sha256"]), 64)
        self.assertEqual(self.library.get_chunk(chunks[0]["chunk_id"])["title"], "note")

    def test_filters_apply_before_ranking_and_no_evidence(self):
        self.library.import_document(self.document("ko.md", "# 방법\n\n결측치 대치는 학습 분할 안에서 적합한다."), {"language":"ko", "domain":"validation"})
        self.library.import_document(self.document("en.md", "# Method\n\nImputation is fitted in a training fold."), {"language":"en", "domain":"validation"})
        result = self.library.search("결측치 대치", lang="ko", domain="validation")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(all(hit["source_id"].startswith("src_" + self.library.project_id) for hit in result["hits"]))
        self.assertEqual(self.library.search("결측치 대치", lang="en")["status"], "no_evidence")
        self.assertEqual(self.library.search_log(result["search_id"])["settings"]["score_meaning"], "relevance_not_probability")

    def test_revocation_and_delete_invalidate_retrieval(self):
        source = self.library.import_document(self.document(), {"language":"en"})
        chunk_id = self.library.list_chunks(source["source_id"])[0]["chunk_id"]
        result = self.library.search("preprocessing", lang="en")
        searched_chunk_id = result["hits"][0]["chunk_id"]
        self.library.set_active(source["source_id"], False)
        self.assertEqual(self.library.search_log(result["search_id"])["hits"][0]["currently_valid"], 0)
        with self.assertRaises(PermissionError):
            self.library.get_chunk(chunk_id)
        self.assertEqual(self.library.search("preprocessing", lang="en")["status"], "no_evidence")
        self.library.set_active(source["source_id"], True)
        tombstone = self.library.delete_source(source["source_id"])
        self.assertEqual(tombstone["extraction_status"], "deleted")
        self.assertIsNone(tombstone["stored_path"])
        with self.assertRaises(KeyError):
            self.library.get_chunk(chunk_id)
        audit_hit = self.library.search_log(result["search_id"])["hits"][0]
        self.assertEqual(audit_hit["chunk_id"], searched_chunk_id)
        self.assertEqual(audit_hit["currently_valid"], 0)

    def test_delete_lock_failure_is_inactive_and_retryable_without_leaking_error_text(self):
        source = self.library.import_document(self.document(), {"language": "en"})
        chunk_id = self.library.list_chunks(source["source_id"])[0]["chunk_id"]
        stored = self.project / source["stored_path"]

        with patch.object(Path, "unlink", side_effect=PermissionError("private path must not be logged")):
            pending = self.library.delete_source(source["source_id"])

        self.assertFalse(pending["active"])
        self.assertEqual(pending["extraction_status"], "purge_pending")
        self.assertEqual(pending["extraction_error"], "purge_failed:PermissionError")
        self.assertNotIn("private path", pending["extraction_error"])
        self.assertTrue(stored.exists())
        self.assertEqual(self.library.search("preprocessing", lang="en")["status"], "no_evidence")
        with self.assertRaises(KeyError):
            self.library.get_chunk(chunk_id)
        with self.assertRaisesRegex(ValueError, "deleted_source_cannot_be_reactivated"):
            self.library.set_active(source["source_id"], True)

        deleted = self.library.delete_source(source["source_id"])
        self.assertEqual(deleted["extraction_status"], "deleted")
        self.assertIsNone(deleted["stored_path"])
        self.assertIsNone(deleted["stored_sha256"])
        self.assertFalse(stored.exists())

    def test_delete_keeps_shared_copy_for_another_active_source(self):
        path = self.document()
        first = self.library.import_document(path, {"language": "en", "version": "1"})
        second = self.library.import_document(path, {"language": "en", "version": "2"})
        self.assertEqual(first["stored_path"], second["stored_path"])
        second_chunk = self.library.list_chunks(second["source_id"])[0]["chunk_id"]
        stored = self.project / first["stored_path"]

        deleted_first = self.library.delete_source(first["source_id"])
        self.assertEqual(deleted_first["extraction_status"], "deleted")
        self.assertEqual(deleted_first["extraction_error"],
                         "retained_for_other_authorized_source")
        self.assertTrue(stored.exists())
        self.assertEqual(self.library.get_chunk(second_chunk)["source_id"], second["source_id"])

        deleted_second = self.library.delete_source(second["source_id"])
        self.assertEqual(deleted_second["extraction_status"], "deleted")
        self.assertFalse(stored.exists())

    def test_hash_corruption_is_detected(self):
        source = self.library.import_document(self.document())
        chunk_id = self.library.list_chunks(source["source_id"])[0]["chunk_id"]
        stored = self.project / source["stored_path"]
        stored.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stored_document_hash_mismatch"):
            self.library.get_chunk(chunk_id)
        result = self.library.search("preprocessing")
        self.assertEqual(result["status"], "no_evidence")
        exclusions = result["settings"]["candidate_exclusions"]
        self.assertIn(chunk_id, {item["chunk_id"] for item in exclusions})
        self.assertTrue(all("stored_document_hash_mismatch" in item["reason"] for item in exclusions))

    def test_chunk_corruption_is_excluded_before_ranking(self):
        source = self.library.import_document(self.document())
        chunk_id = self.library.list_chunks(source["source_id"])[0]["chunk_id"]
        with self.library.db:
            self.library.db.execute("UPDATE chunks SET text='tampered text' WHERE chunk_id=?", (chunk_id,))
        result = self.library.search("tampered text")
        self.assertEqual(result["status"], "no_evidence")
        excluded = result["settings"]["candidate_exclusions"]
        self.assertEqual(excluded[0]["chunk_id"], chunk_id)
        self.assertEqual(excluded[0]["reason"], "chunk_hash_mismatch")

    def test_chunk_is_project_scoped(self):
        source = self.library.import_document(self.document())
        chunk_id = self.library.list_chunks(source["source_id"])[0]["chunk_id"]
        other_path = self.root / "two.swa"; other_path.mkdir()
        with EvidenceLibrary(other_path) as other:
            with self.assertRaisesRegex(KeyError, "chunk_not_found_for_project"):
                other.get_chunk(chunk_id)

    def test_project_copy_preserves_identity_and_explicitly_copied_evidence(self):
        source = self.library.import_document(self.document())
        chunk_id = self.library.list_chunks(source["source_id"])[0]["chunk_id"]
        identity = self.library.project_id
        self.library.close()
        copied_path = self.root / "copied.swa"
        shutil.copytree(self.project, copied_path)
        with EvidenceLibrary(copied_path) as copied:
            self.assertEqual(copied.project_id, identity)
            self.assertEqual(copied.get_chunk(chunk_id)["chunk_id"], chunk_id)
        self.library = EvidenceLibrary(self.project)

    def test_version_superseding_deactivates_old_only_after_new_import(self):
        old = self.library.import_document(self.document("v1.md", "old method"), {"version":"1"})
        new = self.library.import_document(self.document("v2.md", "new method"),
                                           {"version":"2", "supersedes_source_id":old["source_id"]})
        self.assertFalse(self.library.get_source(old["source_id"], include_inactive=True)["active"])
        self.assertTrue(new["active"])
        self.assertEqual(self.library.search("new method", version="1")["status"], "no_evidence")
        self.assertEqual(self.library.search("new method", version="2")["status"], "ok")

    def test_metadata_and_failed_extraction_do_not_claim_full_text(self):
        metadata = self.library.register_metadata(title="DOI lead", doi="10.0000/example")
        self.assertEqual(metadata["extraction_status"], "metadata_only")
        binary = self.root / "scan.bin"; binary.write_bytes(b"not text")
        failed = self.library.import_document(binary)
        self.assertEqual(failed["extraction_status"], "unsupported")
        self.assertEqual(self.library.search("DOI lead")["status"], "no_evidence")

    @unittest.skipUnless(importlib.util.find_spec("docx"), "python-docx not installed")
    def test_docx_preserves_paragraph_and_table_anchors(self):
        from docx import Document

        path = self.root / "structured.docx"
        document = Document()
        document.add_heading("Methods", level=1)
        document.add_paragraph("Training-fold preprocessing prevents leakage.")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Metric"
        table.cell(0, 1).text = "RMSE"
        document.save(path)

        source = self.library.import_document(path, {"language": "en"})
        chunks = self.library.list_chunks(source["source_id"])
        self.assertEqual(source["extraction_status"], "extracted")
        self.assertTrue(source["extraction_tool"].startswith("python-docx/"))
        self.assertIn("paragraph", {chunk["locator_type"] for chunk in chunks})
        self.assertIn("table_row", {chunk["locator_type"] for chunk in chunks})

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf not installed")
    def test_image_only_pdf_is_marked_for_ocr_without_inventing_text(self):
        from pypdf import PdfWriter

        path = self.root / "scan.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        with path.open("wb") as output:
            writer.write(output)
        source = self.library.import_document(path)
        self.assertEqual(source["extraction_status"], "ocr_needed")
        self.assertEqual(source["extraction_error"], "pdf_has_pages_but_no_extractable_text")
        self.assertEqual(self.library.search("imagined scan content")["status"], "no_evidence")

    def test_evaluation_separates_split_and_reports_denominator(self):
        fixture = ROOT / "evaluation" / "rag_fixture_v1.json"
        result = run_evaluation(self.library, fixture, split="test", top_k=3)
        self.assertEqual(result["denominator"], 6)
        self.assertEqual(result["answerable_denominator"], 4)
        self.assertEqual(result["no_evidence_denominator"], 2)
        self.assertEqual(result["semantic_support_review"], "not_performed")
        self.assertGreaterEqual(result["recall_at_k"], 0.75)
        self.assertGreaterEqual(result["mrr"], 0.75)
        self.assertEqual(result["no_evidence_accuracy"], 1.0)

    def test_snapshot_is_stable_for_same_source_set(self):
        self.library.import_document(self.document())
        one = self.library.snapshot(); two = self.library.snapshot()
        self.assertEqual(one["snapshot_sha256"], two["snapshot_sha256"])


if __name__ == "__main__":
    unittest.main()
