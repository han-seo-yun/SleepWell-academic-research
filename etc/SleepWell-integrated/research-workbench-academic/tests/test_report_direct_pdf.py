import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader

from researchbench.reporting import ReportService
from researchbench.result_adapter import adapt_run


class DirectReportPDFTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "study.swa"
        runs = self.project / "runs"
        runs.mkdir(parents=True)
        self.run_path = runs / "run_direct_pdf.json"
        self.record = {
            "id": "run_direct_pdf",
            "name": "SleepWell PDF fixture",
            "status": "completed",
            "config": {
                "mode": "retrospective", "data_hash": "data-v1",
                "target": "sleep onset", "target_unit": "min",
            },
            "created": "2026-11-04T00:00:00+00:00",
            "duration": 1.0,
            "result": {
                "mode": "retrospective",
                "summary": [
                    {"model_id": "cmi.data", "joint_mae_min": 12.5,
                     "n_nights": 2, "n_participants": 1},
                ],
                "raw": {
                    "predictions": [
                        {"model_id": "cmi.data", "participant_id": "p1",
                         "cutoff": "2026-11-01", "onset_offset_min": 5.0,
                         "wake_offset_min": 8.0, "actual_onset_offset_min": 4.0,
                         "actual_wake_offset_min": 9.0},
                    ],
                },
                "provenance": {"executed": True},
            },
        }
        self.run_path.write_text(json.dumps(self.record, ensure_ascii=False), encoding="utf-8")
        self.view = adapt_run(self.record, "sleepwell", self.project)
        self.service = ReportService(self.project)
        self.report = self.service.create(
            [self.view], template="research", lang="ko", question="수면 예측 모델 평가",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_exports_real_korean_pdf_exclusively_and_cleans_temporary_package(self):
        destination = self.root / "output" / "SleepWell-report.pdf"
        real_temporary_directory = tempfile.TemporaryDirectory
        used_directories = []

        def tracked_temporary_directory(*args, **kwargs):
            value = real_temporary_directory(*args, **kwargs)
            used_directories.append(Path(value.name))
            return value

        with patch("researchbench.reporting.tempfile.TemporaryDirectory", tracked_temporary_directory):
            result = self.service.export_pdf(self.report["report_id"], destination)

        self.assertEqual(result, destination.resolve())
        self.assertTrue(destination.is_file())
        self.assertTrue(destination.read_bytes().startswith(b"%PDF-"))
        self.assertTrue(used_directories)
        self.assertTrue(all(not path.exists() for path in used_directories))
        text = "\n".join(page.extract_text() for page in PdfReader(destination).pages)
        self.assertIn("연구 근거 보고서", text)
        self.assertIn("초안", text)
        self.assertIn("cmi.data", text)
        self.assertEqual(self.service.get(self.report["report_id"]), self.report)
        with self.assertRaisesRegex(FileExistsError, "report_pdf_already_exists"):
            self.service.export_pdf(self.report["report_id"], destination)
        with self.assertRaisesRegex(ValueError, "report_destination_must_be_pdf"):
            self.service.export_pdf(self.report["report_id"], self.root / "report.docx")

    def test_checked_mode_does_not_bypass_blocking_report_check(self):
        changed = dict(self.record)
        changed["result"] = dict(self.record["result"])
        changed["result"]["summary"] = [
            {"model_id": "cmi.data", "joint_mae_min": 99.0,
             "n_nights": 2, "n_participants": 1},
        ]
        self.run_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        destination = self.root / "checked.pdf"
        with self.assertRaisesRegex(ValueError, "blocking_report_check_errors"):
            self.service.export_pdf(self.report["report_id"], destination, mode="checked")
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
