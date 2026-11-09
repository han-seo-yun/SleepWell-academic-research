import copy
import concurrent.futures
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from researchbench.evidence_library import EvidenceLibrary
from researchbench.report_export import _build_prediction_figure
from researchbench.reporting import ReportService
from researchbench.result_adapter import adapt_run, model_color


class ReportServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "study.swa"
        (self.project / "experiments").mkdir(parents=True)
        self.record = {
            "id": "run_baseline",
            "name": "Baseline",
            "status": "completed",
            "config": {
                "model": "random_forest",
                "task": "regression",
                "target": "score",
                "target_unit": "point",
                "data_hash": "data-v1",
            },
            "result": {
                "task": "regression",
                "phase": "validation",
                "summary": {"rmse": {"mean": 1.25, "std": 0.1, "n": 3}},
                "folds": [{"reasons": {}}],
                "predictions": [
                    {"row_id": "a", "y_true": 1.0, "y_pred": 1.2},
                    {"row_id": "b", "y_true": 2.0, "y_pred": 1.9},
                ],
                "split_id": "split-001",
                "provenance": {"device": "CPU", "versions": {"scikit-learn": "fixture"}},
            },
        }
        self.run_path = self.project / "experiments" / "run_baseline.json"
        self.run_path.write_text(json.dumps(self.record, ensure_ascii=False), encoding="utf-8")
        self.view = adapt_run(self.record, "general", self.project)
        self.library = EvidenceLibrary(self.project)
        document = self.root / "method.md"
        document.write_text("# Methods\n\nImputation is fitted inside each training fold to avoid leakage.", encoding="utf-8")
        self.source = self.library.import_document(document, {"language": "en", "version": "1"})
        result = self.library.search("imputation training fold", lang="en")
        self.assertEqual(result["status"], "ok")
        self.citation = dict(result["hits"][0])
        self.citation["search_id"] = result["search_id"]
        self.service = ReportService(self.project, self.library)

    def tearDown(self):
        self.library.close()
        self.temp.cleanup()

    def create(self, lang="ko"):
        return self.service.create(
            [self.view],
            template="research",
            lang=lang,
            question="Does preprocessing avoid leakage?",
            context={"purpose": "General ML validation", "api_key": "must-not-be-stored"},
            citations=[self.citation],
        )

    def test_create_saves_immutable_structured_report_and_filters_secrets(self):
        report = self.create()
        restored = self.service.get(report["report_id"])
        self.assertEqual(restored["artifact_sha256"], report["artifact_sha256"])
        self.assertNotIn("api_key", restored["context"])
        numeric = next(claim for claim in restored["claims"] if claim["claim_type"] == "experiment_fact")
        self.assertEqual(numeric["evidence"]["field_path"], ["result", "summary", "rmse", "mean"])
        self.assertEqual(numeric["evidence"]["value"], 1.25)
        self.assertEqual(self.service.check(restored)["state"], "automatic_check_passed")
        changed = copy.deepcopy(restored)
        changed["question"] = "overwrite"
        with self.assertRaisesRegex(FileExistsError, "immutable_report_version"):
            self.service.save(changed)

    def test_numeric_text_tamper_and_changed_run_are_blocking(self):
        report = self.create()
        tampered = copy.deepcopy(report)
        numeric = next(claim for claim in tampered["claims"] if claim["claim_type"] == "experiment_fact")
        numeric["text"] = numeric["text"].replace("1.25", "9.99")
        codes = {item["code"] for item in self.service.check(tampered)["errors"]}
        self.assertIn("numeric_text_mismatch", codes)
        extra_number = copy.deepcopy(report)
        extra_claim = next(claim for claim in extra_number["claims"] if claim["claim_type"] == "experiment_fact")
        extra_claim["text"] += " 추가 수치 99."
        extra_codes = {item["code"] for item in self.service.check(extra_number)["errors"]}
        self.assertIn("unexpected_numeric_text", extra_codes)

        changed_record = copy.deepcopy(self.record)
        changed_record["result"]["summary"]["rmse"]["mean"] = 8.0
        self.run_path.write_text(json.dumps(changed_record), encoding="utf-8")
        changed_codes = {item["code"] for item in self.service.check(report)["errors"]}
        self.assertIn("origin_run_changed", changed_codes)

    def test_linked_general_run_is_frozen_inside_report_project_and_counts_are_canonical(self):
        report_project = self.root / "academic-report.swa"
        report_project.mkdir()
        linked = ReportService(report_project)
        report = linked.create([self.view], template="comparison", lang="en")
        run_ref = report["run_refs"][0]
        frozen = report_project / run_ref["frozen_relative_path"]
        self.assertTrue(frozen.is_file())
        self.assertTrue(frozen.resolve().is_relative_to(report_project.resolve()))
        self.assertEqual(linked.check(report)["state"], "automatic_check_passed")

        tampered = copy.deepcopy(report)
        numeric = next(claim for claim in tampered["claims"] if claim["claim_type"] == "experiment_fact")
        numeric["evidence"]["unit"] = "forged-unit"
        numeric["evidence"]["n_rows"] = 999
        numeric["evidence"]["n_participants"] = 999
        codes = {item["code"] for item in linked.check(tampered)["errors"]}
        self.assertIn("canonical_metric_unit_mismatch", codes)
        self.assertIn("canonical_metric_n_rows_mismatch", codes)
        self.assertIn("canonical_metric_n_participants_mismatch", codes)

        self.run_path.unlink()
        portable = linked.check(report)
        self.assertEqual(portable["state"], "automatic_check_passed")
        self.assertIn(
            "origin_run_unavailable_using_frozen_copy",
            {item["code"] for item in portable["warnings"]},
        )

    def test_fabricated_and_revoked_literature_evidence_fail(self):
        report = self.create()
        fabricated = copy.deepcopy(report)
        literature = next(claim for claim in fabricated["claims"] if claim["claim_type"] == "literature_retrieval")
        literature["evidence"]["chunk_id"] = "chunk_fabricated"
        codes = {item["code"] for item in self.service.check(fabricated)["errors"]}
        self.assertIn("literature_evidence_invalid", codes)

        stored = self.project / self.source["stored_path"]
        original_bytes = stored.read_bytes()
        stored.write_text("changed source content", encoding="utf-8")
        changed_codes = {item["code"] for item in self.service.check(report)["errors"]}
        self.assertIn("literature_evidence_invalid", changed_codes)
        stored.write_bytes(original_bytes)

        self.library.set_active(self.source["source_id"], False)
        revoked_codes = {item["code"] for item in self.service.check(report)["errors"]}
        self.assertIn("literature_evidence_invalid", revoked_codes)

    def test_literature_claim_fails_closed_without_project_library(self):
        report = self.create()
        disconnected = ReportService(self.project, library=None)
        result = disconnected.check(report)
        self.assertEqual(result["state"], "automatic_check_failed")
        self.assertIn("evidence_library_unavailable", {item["code"] for item in result["errors"]})
        with self.assertRaisesRegex(ValueError, "blocking_report_check_errors"):
            disconnected.export(report["report_id"], self.root / "no-library", formats=("md", "json"), mode="checked")

    def test_translation_creates_new_version_with_same_facts(self):
        korean = self.create("ko")
        english = self.service.translate(korean["report_id"], "en")
        self.assertNotEqual(korean["report_id"], english["report_id"])
        self.assertEqual(english["parent_report_id"], korean["report_id"])
        korean_evidence = [claim["evidence"] for claim in korean["claims"]]
        english_evidence = [claim["evidence"] for claim in english["claims"]]
        self.assertEqual(korean_evidence, english_evidence)
        self.assertIn("random_forest rmse is 1.25", english["claims"][0]["text"])
        self.assertEqual(self.service.get(korean["report_id"])["language"], "ko")
        self.assertEqual(self.service.check(english)["state"], "automatic_check_passed")

    def test_model_card_checklist_records_all_five_states_and_valid_claim_links(self):
        report = self.service.create(
            [self.view],
            template="model_card",
            lang="en",
            context={
                "checklists": {
                    "card_details": {"status": "satisfied", "note": "Run hash recorded."},
                    "card_intended_use": {"status": "partially_satisfied"},
                    "card_factors": {"status": "not_satisfied"},
                    "card_metrics": {"status": "not_applicable"},
                    "card_data": {"status": "judgment_pending"},
                }
            },
        )
        frameworks = {item["framework_id"]: item for item in report["checklists"]}
        self.assertIn("nist_ai_rmf_1_0", frameworks)
        self.assertIn("model_cards", frameworks)
        card_items = frameworks["model_cards"]["items"]
        self.assertTrue(
            {"satisfied", "partially_satisfied", "not_satisfied", "not_applicable", "judgment_pending"}
            <= {item["status"] for item in card_items}
        )
        metric_item = next(item for item in card_items if item["item_id"] == "card_metrics")
        self.assertTrue(metric_item["evidence_claim_ids"])
        self.assertTrue(
            set(metric_item["evidence_claim_ids"]) <= {claim["claim_id"] for claim in report["claims"]}
        )
        self.assertEqual(self.service.check(report)["state"], "automatic_check_passed")

    def test_external_selected_is_not_external_validation_and_synthetic_caveat_is_mandatory(self):
        run_ref = {
            "run_id": "selected-after-external-review",
            "data_hash": "data-v1",
            "result_hash": "result-v1",
            "evaluation": {
                "evaluation_type": "external_selected",
                "synthetic": True,
                "scope_ids": ["row-1"],
            },
        }
        sections = self.service._sections("research", "en", "Question", {}, [], [run_ref])
        limitations = next(item for item in sections if item["section_id"] == "limitations")["paragraphs"]
        self.assertIn(self.service._t("en", "no_external_validation"), limitations)
        self.assertIn(self.service._t("en", "synthetic_limitation"), limitations)

        external = copy.deepcopy(run_ref)
        external["evaluation"]["evaluation_type"] = "external"
        external_sections = self.service._sections("research", "en", "Question", {}, [], [external])
        external_limitations = next(item for item in external_sections if item["section_id"] == "limitations")["paragraphs"]
        self.assertNotIn(self.service._t("en", "no_external_validation"), external_limitations)
        self.assertIn(self.service._t("en", "synthetic_limitation"), external_limitations)

    def test_revise_and_review_are_new_versions_and_require_recheck(self):
        original = self.create()
        claim_id = next(claim["claim_id"] for claim in original["claims"] if claim["claim_type"] == "experiment_fact")
        revised = self.service.revise(original["report_id"], {"claims": {claim_id: "RMSE는 9.0입니다."}})
        self.assertEqual(revised["automatic_check"]["state"], "not_run")
        self.assertEqual(revised["claims"][0]["review_state"], "pending_recheck")
        self.assertNotEqual(original["report_id"], revised["report_id"])
        reviewed = self.service.record_review(original["report_id"], "Reviewer A", "Checked source location")
        self.assertFalse(reviewed["human_reviews"][-1]["certification"])
        self.assertEqual(self.service.get(original["report_id"])["human_reviews"], [])

    def test_edited_section_cannot_smuggle_unregistered_numeric_claim_into_checked_export(self):
        original = self.create()
        revised = self.service.revise(
            original["report_id"],
            {"sections": {"results": "검증 ROC-AUC는 0.999이다."}},
        )
        codes = {item["code"] for item in self.service.check(revised)["errors"]}
        self.assertIn("edited_section_requires_structured_revalidation", codes)
        reviewed = self.service.record_review(revised["report_id"], "Reviewer A", "Narrative read")
        reviewed_codes = {item["code"] for item in self.service.check(reviewed)["errors"]}
        self.assertIn("edited_section_requires_structured_revalidation", reviewed_codes)
        with self.assertRaisesRegex(ValueError, "blocking_report_check_errors"):
            self.service.export(reviewed["report_id"], self.root / "unsafe", formats=("md",), mode="checked")

        translated = self.service.translate(revised["report_id"], "en")
        result_section = next(item for item in translated["sections"] if item["section_id"] == "results")
        self.assertEqual(result_section["paragraphs"], ["검증 ROC-AUC는 0.999이다."])
        self.assertTrue(result_section["edited_by_user"])

    def test_draft_export_is_visibly_marked_and_checked_export_blocks_errors(self):
        report = self.create("en")
        revised = self.service.revise(
            report["report_id"],
            {"claims": {report["claims"][0]["claim_id"]: "random_forest rmse is 99 point."}},
        )
        paths = self.service.export(
            revised["report_id"], self.root / "exports", formats=("md", "json", "csv", "bib", "figures"), mode="draft"
        )
        markdown = paths["markdown"].read_text(encoding="utf-8")
        self.assertIn("DRAFT", markdown)
        audit = json.loads(paths["audit_json"].read_text(encoding="utf-8"))
        self.assertEqual(audit["export_check"]["state"], "automatic_check_failed")
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        self.assertFalse(manifest["generation"]["llm_used"])
        self.assertEqual(manifest["privacy"]["raw_participant_records_included"], False)
        self.assertFalse(manifest["privacy"]["evaluation_scope_identifiers_included"])
        self.assertTrue(manifest["generation_code"]["files_sha256"])
        self.assertEqual(manifest["retrieval_records"][0]["query"], "imputation training fold")
        self.assertIn("rank", manifest["retrieval_records"][0]["hits"][0])
        self.assertNotIn("text", manifest["retrieval_records"][0]["hits"][0])
        self.assertNotIn("scope_ids", audit["report"]["run_refs"][0]["evaluation"])
        self.assertTrue(paths["figures"].joinpath("run_baseline_predictions.png").exists())
        with self.assertRaisesRegex(ValueError, "blocking_report_check_errors"):
            self.service.export(revised["report_id"], self.root / "checked", formats=("md",), mode="checked")

    @unittest.skipUnless(
        importlib.util.find_spec("docx") and importlib.util.find_spec("reportlab"),
        "python-docx/reportlab not installed",
    )
    def test_docx_and_pdf_exports_are_real_documents(self):
        report = self.create("ko")
        paths = self.service.export(
            report["report_id"], self.root / "office", formats=("md", "docx", "pdf", "figures"), mode="checked"
        )
        self.assertEqual(paths["docx"].read_bytes()[:2], b"PK")
        self.assertEqual(paths["pdf"].read_bytes()[:4], b"%PDF")
        self.assertGreater(paths["pdf"].stat().st_size, 1000)
        with zipfile.ZipFile(paths["docx"]) as archive:
            self.assertTrue(any(name.startswith("word/media/") for name in archive.namelist()))
            document_xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("실험 지표 표", document_xml)
            self.assertIn("자동 검사", document_xml)
        markdown = paths["markdown"].read_text(encoding="utf-8")
        self.assertIn("| 모델 | 지표 | 값 |", markdown)
        self.assertIn("![실제값–예측값](figures/run_baseline_predictions.png)", markdown)
        for suffix in ("png", "pdf", "svg"):
            self.assertTrue(paths["figures"].joinpath(f"run_baseline_predictions.{suffix}").is_file())

    def test_sleepwell_prediction_figure_uses_common_scope_stable_colors_and_minutes(self):
        from matplotlib.colors import to_hex

        canonical = {
            "engine": "sleepwell",
            "evaluation": {"task": "sleep_timing", "scope_ids": [["p1", "0"], ["p1", "1"]]},
            "config": {},
            "predictions": [
                {"participant_id": "p1", "cutoff": 0, "model_id": "model_b", "actual_onset_offset_min": 10, "onset_offset_min": 11},
                {"participant_id": "p1", "cutoff": 1, "model_id": "model_b", "actual_onset_offset_min": 20, "onset_offset_min": 19},
                {"participant_id": "p1", "cutoff": 0, "model_id": "model_a", "actual_onset_offset_min": 10, "onset_offset_min": 12},
                {"participant_id": "p1", "cutoff": 1, "model_id": "model_a", "actual_onset_offset_min": 20, "onset_offset_min": 18},
                {"participant_id": "p1", "cutoff": 2, "model_id": "model_a", "actual_onset_offset_min": 30, "onset_offset_min": 31},
                {"participant_id": "p1", "cutoff": 0, "model_id": "model_a", "actual_onset_offset_min": 10, "onset_offset_min": float("inf")},
            ],
        }
        figure = _build_prediction_figure(canonical, "en", self.service.locales["en"])
        self.assertIsNotNone(figure)
        axis = figure.axes[0]
        self.assertEqual(axis._researchbench_scope_n, 2)
        self.assertEqual(axis._researchbench_point_n, 4)
        self.assertEqual(
            axis._researchbench_model_colors,
            {"model_a": model_color("model_a"), "model_b": model_color("model_b")},
        )
        plotted_colors = [to_hex(collection.get_facecolors()[0]) for collection in axis.collections]
        self.assertEqual(plotted_colors, [model_color("model_a").lower(), model_color("model_b").lower()])
        self.assertEqual(axis.get_xlabel(), "Sleep onset offset (min)")
        self.assertEqual(axis.get_ylabel(), "Sleep onset offset (min)")
        figure.clear()

    def test_regression_prediction_figure_preserves_zero_row_id_and_filters_nonfinite(self):
        canonical = {
            "engine": "general",
            "evaluation": {"task": "regression", "scope_ids": ["0", "1"]},
            "config": {"model": "linear", "target_unit": "kg"},
            "predictions": [
                {"row_id": 0, "y_true": 1, "y_pred": 1.1},
                {"row_id": 1, "y_true": 2, "y_pred": 1.9},
                {"row_id": 2, "y_true": 3, "y_pred": 3.1},
                {"row_id": 0, "y_true": float("nan"), "y_pred": 1.1},
            ],
        }
        figure = _build_prediction_figure(canonical, "en", self.service.locales["en"])
        self.assertIsNotNone(figure)
        axis = figure.axes[0]
        self.assertEqual(axis._researchbench_scope_n, 2)
        self.assertEqual(axis._researchbench_point_n, 2)
        self.assertEqual(axis.get_xlabel(), "Observed (kg)")
        self.assertEqual(axis.get_ylabel(), "Predicted (kg)")
        figure.clear()

    def test_classification_prediction_figure_is_confusion_matrix_with_raw_labels(self):
        canonical = {
            "engine": "general",
            "evaluation": {"task": "classification", "scope_ids": ["0", "1", "2"]},
            "config": {"model": "classifier"},
            "predictions": [
                {"row_id": 0, "y_true": "control", "y_pred": "control"},
                {"row_id": 1, "y_true": "case", "y_pred": "control"},
                {"row_id": 2, "y_true": "case", "y_pred": "case"},
                {"row_id": 3, "y_true": "control", "y_pred": "case"},
            ],
        }
        figure = _build_prediction_figure(canonical, "en", self.service.locales["en"])
        self.assertIsNotNone(figure)
        axis = figure.axes[0]
        self.assertEqual(axis._researchbench_scope_n, 3)
        self.assertEqual(axis._researchbench_confusion, [[1, 1], [0, 1]])
        self.assertEqual([label.get_text() for label in axis.get_xticklabels()], ["case", "control"])
        self.assertEqual(axis.get_xlabel(), "Predicted class")
        self.assertEqual(axis.get_ylabel(), "Actual class")
        figure.clear()

    @unittest.skipUnless(importlib.util.find_spec("reportlab") and importlib.util.find_spec("pypdf"), "PDF stack not installed")
    def test_concurrent_english_pdf_exports_keep_complete_font_subsets(self):
        if os.environ.get("RESEARCHBENCH_CONCURRENT_PDF_TEST_CHILD") != "1":
            environment = dict(os.environ)
            environment["RESEARCHBENCH_CONCURRENT_PDF_TEST_CHILD"] = "1"
            completed = subprocess.run(
                [
                    sys.executable, "-X", "utf8", "-m", "unittest",
                    "tests.test_reporting.ReportServiceTests.test_concurrent_english_pdf_exports_keep_complete_font_subsets",
                    "-v",
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=180,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)
            return

        from pypdf import PdfReader

        report = self.service.create([self.view], template="research", lang="en", question="Concurrent PDF subset test")
        pdf_service = ReportService(self.project, library=None)

        def export(index):
            return pdf_service.export(
                report["report_id"], self.root / f"concurrent-{index}", formats=("pdf",), mode="checked"
            )["pdf"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(export, range(4)))
        expected = ("run_baseline", "random_forest", report["claims"][0]["claim_id"], "Automatic check passed")
        for path in paths:
            text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
            for token in expected:
                self.assertIn(token, text)

    @unittest.skipUnless(shutil.which("pdftoppm") and importlib.util.find_spec("PIL"), "Poppler/Pillow not installed")
    def test_sequential_mixed_language_pdf_rasters_are_stable(self):
        from PIL import Image, ImageChops

        baseline = self.service.create(
            [self.view], template="comparison", lang="en",
            context={"purpose": "Preserve 원본 사용자 제목 without changing evidence."},
        )
        other_en = self.service.create([self.view], template="industrial", lang="en", context={"purpose": "English"})
        other_ko = self.service.create([self.view], template="model_card", lang="ko", context={"purpose": "한국어"})

        def export(report, name):
            return self.service.export(report["report_id"], self.root / name, formats=("pdf",), mode="checked")["pdf"]

        first = export(baseline, "raster-first")
        export(other_en, "raster-middle-en")
        export(other_ko, "raster-middle-ko")
        last = export(baseline, "raster-last")

        def raster(pdf, name):
            target = self.root / name
            subprocess.run(
                [shutil.which("pdftoppm"), "-f", "1", "-l", "1", "-singlefile", "-png", "-scale-to", "1000", str(pdf), str(target)],
                check=True, capture_output=True,
            )
            return Image.open(target.with_suffix(".png")).convert("RGB")

        before = raster(first, "before")
        after = raster(last, "after")
        self.assertEqual(before.size, after.size)
        self.assertIsNone(ImageChops.difference(before, after).getbbox())


if __name__ == "__main__":
    unittest.main()
