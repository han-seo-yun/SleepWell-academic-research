import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from researchbench import guideline_library


PDF_STACK = bool(importlib.util.find_spec("reportlab") and importlib.util.find_spec("pypdf"))


@unittest.skipUnless(PDF_STACK, "reportlab and pypdf are required")
class GuidelineLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.resources = self.root / "resources"
        self.bundle = self.resources / "official_guidelines" / "tripod.pdf"
        self.bundle.parent.mkdir(parents=True)
        self._make_pdf(self.bundle, "Official test original")
        digest = hashlib.sha256(self.bundle.read_bytes()).hexdigest()
        self.catalog_path = self.resources / "guideline_library.json"
        frameworks = []
        for framework_id, name, year in (
            ("tripod_ai", "TRIPOD+AI", 2024),
            ("probast_ai", "PROBAST+AI", 2025),
            ("decide_ai", "DECIDE-AI", 2022),
            ("v3_plus", "V3+", 2025),
        ):
            frameworks.append({
                "id": framework_id,
                "name": name,
                "year": year,
                "role_ko": f"{name} 역할",
                "role_en": f"Role of {name}",
                "application_ko": "SleepWell 적용",
                "application_en": "SleepWell application",
                "scope_ko": "적용 범위",
                "scope_en": "Application scope",
                "official_url": f"https://example.org/{framework_id}",
            })
        self.document = {
            "id": "tripod_ai_checklist",
            "framework": "tripod_ai",
            "title_ko": "TRIPOD+AI 체크리스트",
            "title_en": "TRIPOD+AI checklist",
            "year": 2024,
            "url": "https://example.org/tripod.pdf",
            "official_url": "https://example.org/tripod.pdf",
            "filename": "TRIPOD_AI_checklist.pdf",
            "sha256": digest,
            "bundled_path": "official_guidelines/tripod.pdf",
            "version": "2024",
            "verified_on": "2026-11-04",
            "acquisition_url": "https://example.org/archive/supplementaryFiles",
            "license": "Retain the official attribution.",
            "notes_ko": "공식 원문 시험 자료",
            "notes_en": "Official-original test material",
        }
        self._write_catalog(frameworks, [self.document])
        self.cache = self.root / "cache"
        self.patchers = (
            mock.patch.object(guideline_library, "RESOURCE_ROOT", self.resources),
            mock.patch.object(guideline_library, "CATALOG_PATH", self.catalog_path),
        )
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    @staticmethod
    def _make_pdf(path: Path, text: str) -> None:
        from reportlab.pdfgen.canvas import Canvas

        canvas = Canvas(str(path))
        canvas.drawString(72, 760, text)
        canvas.save()

    def _write_catalog(self, frameworks, documents) -> None:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path.write_text(json.dumps({
            "schema": "sleepwell.guideline_library.v1",
            "frameworks": frameworks,
            "documents": documents,
        }, ensure_ascii=False), encoding="utf-8")

    def test_catalog_status_and_offline_obtain_verify_bundle(self):
        loaded = guideline_library.catalog()
        self.assertEqual(loaded["schema"], "sleepwell.guideline_library.v1")
        self.assertEqual(len(loaded["frameworks"]), 4)

        status = guideline_library.resource_status(self.document["id"])
        self.assertTrue(status["available"])
        self.assertTrue(status["valid"])
        result = guideline_library.obtain(self.document["id"], self.cache)
        self.assertEqual(result["source"], "bundled")
        self.assertIsNone(result["warning"])
        self.assertEqual(hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest(), self.document["sha256"])
        metadata = Path(result["path"]).with_suffix(".json")
        metadata_value = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertEqual(metadata_value["sha256"], self.document["sha256"])
        self.assertEqual(metadata_value["source"], "bundled")
        self.assertEqual(metadata_value["source_semantics"], "initial_cache_creation")
        receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["retrieval_source"], "bundled")

    def test_network_receipt_records_current_source_without_rewriting_initial_metadata(self):
        initial = guideline_library.obtain(self.document["id"], self.cache)
        initial_metadata_path = Path(initial["path"]).with_suffix(".json")
        initial_metadata_bytes = initial_metadata_path.read_bytes()
        pdf_bytes = self.bundle.read_bytes()
        response = mock.MagicMock()
        response.headers = {"Content-Length": str(len(pdf_bytes)), "Content-Type": "application/pdf"}
        response.geturl.return_value = self.document["url"]
        response.read.side_effect = [pdf_bytes, b""]
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        with mock.patch.object(guideline_library, "urlopen", return_value=response):
            current = guideline_library.obtain(self.document["id"], self.cache, prefer_network=True)

        self.assertEqual(current["source"], "network")
        self.assertEqual(current["retrieval_source"], "network")
        self.assertEqual(set(current), set(initial))
        receipt = json.loads(Path(current["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["retrieval_source"], "network")
        self.assertEqual(receipt["url"], self.document["url"])
        self.assertEqual(receipt["acquisition_url"], self.document["acquisition_url"])
        self.assertEqual(initial_metadata_path.read_bytes(), initial_metadata_bytes)
        metadata = json.loads(initial_metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["source"], "bundled")
        self.assertEqual(metadata["source_semantics"], "initial_cache_creation")

    def test_missing_bundle_falls_back_to_cache_with_receipt_and_save_all_succeeds(self):
        bundled = guideline_library.obtain(self.document["id"], self.cache)
        self.bundle.unlink()

        cached = guideline_library.obtain(self.document["id"], self.cache)

        self.assertEqual(set(cached), set(bundled))
        self.assertEqual(cached["source"], "cache")
        self.assertEqual(cached["retrieval_source"], "cache")
        self.assertIn("previously verified cache", cached["warning"])
        receipt = json.loads(Path(cached["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["retrieval_source"], "cache")
        self.assertEqual(receipt["warning"], cached["warning"])

        destination = self.root / "cache-only-export"
        copied = guideline_library.save_all(destination, self.cache)
        self.assertEqual(copied[0]["retrieval_source"], "cache")
        self.assertEqual(
            (destination / self.document["filename"]).read_bytes(),
            Path(bundled["path"]).read_bytes(),
        )
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["documents"][0]["retrieval_source"], "cache")

    def test_network_failure_uses_verified_bundle_with_plain_warning(self):
        with mock.patch.object(guideline_library, "urlopen", side_effect=URLError("offline")):
            result = guideline_library.obtain(self.document["id"], self.cache, prefer_network=True)
        self.assertEqual(result["source"], "bundled")
        self.assertIn("could not be verified", result["warning"])
        self.assertNotIn("download succeeded", result["warning"].lower())

    def test_fake_html_download_is_rejected_and_bundle_is_used(self):
        response = mock.MagicMock()
        response.headers = {"Content-Length": "31", "Content-Type": "text/html"}
        response.geturl.return_value = "https://example.org/tripod.pdf"
        response.read.side_effect = [b"<html>not a pdf</html>", b""]
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch.object(guideline_library, "urlopen", return_value=response) as opened:
            result = guideline_library.obtain(self.document["id"], self.cache, prefer_network=True)
        self.assertEqual(result["source"], "bundled")
        self.assertIn("not_pdf", result["warning"])
        self.assertEqual(opened.call_args.args[0].full_url, self.document["url"])

    def test_cache_conflict_is_not_overwritten(self):
        result = guideline_library.obtain(self.document["id"], self.cache)
        cache_path = Path(result["path"])
        cache_path.write_bytes(b"different prior bytes")
        with self.assertRaises(ValueError):
            guideline_library.obtain(self.document["id"], self.cache)
        self.assertEqual(cache_path.read_bytes(), b"different prior bytes")

    def test_copy_original_preserves_bytes_and_refuses_overwrite(self):
        destination = self.root / "saved" / "original.pdf"
        copied = guideline_library.copy_original(self.document["id"], destination, self.cache)
        self.assertEqual(destination.read_bytes(), self.bundle.read_bytes())
        self.assertEqual(copied["path"], str(destination.resolve()))
        with self.assertRaises(FileExistsError):
            guideline_library.copy_original(self.document["id"], destination, self.cache)

    def test_save_all_writes_manifest_and_refuses_nonempty_destination(self):
        destination = self.root / "all"
        copied = guideline_library.save_all(destination, self.cache)
        self.assertEqual(len(copied), 1)
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        manifest_document = manifest["documents"][0]
        self.assertEqual(manifest_document["sha256"], self.document["sha256"])
        self.assertEqual(manifest_document["url"], self.document["url"])
        self.assertEqual(manifest_document["official_url"], self.document["official_url"])
        self.assertEqual(manifest_document["acquisition_url"], self.document["acquisition_url"])
        self.assertEqual(manifest_document["version"], self.document["version"])
        self.assertEqual(manifest_document["license"], self.document["license"])
        self.assertEqual(manifest_document["year"], self.document["year"])
        self.assertEqual(manifest_document["retrieval_source"], "bundled")
        with self.assertRaises(FileExistsError):
            guideline_library.save_all(destination, self.cache)

    def test_invalid_bundle_hash_is_reported_unavailable(self):
        self.bundle.write_bytes(b"%PDF-damaged")
        status = guideline_library.resource_status(self.document["id"])
        self.assertFalse(status["available"])
        self.assertFalse(status["valid"])
        with self.assertRaises(ValueError):
            guideline_library.obtain(self.document["id"], self.cache)

    def test_export_guide_pdf_has_korean_text_and_required_caveats(self):
        from pypdf import PdfReader

        destination = self.root / "SleepWell_지침_안내서.pdf"
        result = guideline_library.export_guide_pdf(destination, language="ko")
        self.assertEqual(result, destination.resolve())
        extracted = "\n".join(page.extract_text() or "" for page in PdfReader(destination).pages)
        self.assertIn("SleepWell 공식 지침 적용 안내서", extracted)
        self.assertIn("공식 지침 원문이 아닙니다", extracted)
        self.assertIn("후향적 외부검증", extracted)
        self.assertIn("전향적 검증", extracted)
        self.assertIn("[TRIPOD+AI] TRIPOD+AI 체크리스트", extracted)
        self.assertIn("문서 PDF URL", extracted)
        self.assertIn("확보 출처", extracted)
        self.assertIn("라이선스·권리 안내", extracted)
        for name in ("TRIPOD+AI", "PROBAST+AI", "DECIDE-AI", "V3+"):
            self.assertIn(name, extracted)
        with self.assertRaises(FileExistsError):
            guideline_library.export_guide_pdf(destination, language="ko")


if __name__ == "__main__":
    unittest.main()
