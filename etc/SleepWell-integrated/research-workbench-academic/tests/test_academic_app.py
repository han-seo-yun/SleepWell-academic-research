from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from researchbench.academic_app import AcademicApp
from researchbench.academic_backend import run_research
from researchbench.academic_store import AcademicProject, timestamp


class AcademicAppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_research({
            "mode": "retrospective", "input_mode": "synthetic",
            "cohorts": ["cmi", "mesa"],
            "synthetic": {
                "n_nights": 5, "seed": 23, "start_date": "2026-01-01",
                "bed_hour": 23.5, "duration_hours": 8,
                "jitter_minutes": 15, "phase_shift_minutes": 30,
                "shift_after_night": 3, "missing_every": 0,
                "report_delay_hours": 1,
            },
        })

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = AcademicProject.create(Path(self.temp.name) / "academic.swa", "GUI smoke")
        record = self.project.new_record({
            "mode": "retrospective", "name": "Original user title",
            "input_synthetic": True, "max_seconds": 60,
        })
        record.update(status="completed", stage="completed", duration=.1,
                      finished=timestamp(), result=self.result)
        self.project.save_run(record)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = AcademicApp(self.root, self.project, language="ko", preferences=False)

    def tearDown(self):
        self.root.update_idletasks()
        self.app.close()
        self.temp.cleanup()

    def test_result_table_and_plot_use_real_nine_arm_envelope(self):
        self.app.navigate("results")
        self.assertEqual(len(self.app.result_tree.get_children()), 9)
        values = [self.app.result_tree.item(item, "values")
                  for item in self.app.result_tree.get_children()]
        self.assertIn("CMI · 수리 고정", [row[0] for row in values])
        self.assertIsNotNone(self.app.figure)
        self.assertEqual(len(self.app.figure.axes[0].patches), 9)
        self.app.var("personalization_field").set("sleep_threshold")
        self.app.var("corr_method").set("spearman")
        options = self.app.options()
        self.assertEqual(options["personalization_field"], "sleep_threshold")
        self.assertEqual(options["corr_method"], "spearman")
        table_holder = self.app.result_tree.master
        self.app.var("show_table").set("0")
        self.app.toggle_result_table()
        self.assertEqual(table_holder.winfo_manager(), "")
        self.app.var("show_table").set("1")
        self.app.toggle_result_table()
        self.assertEqual(table_holder.winfo_manager(), "pack")

    def test_language_rebuild_preserves_user_text_and_localizes_result(self):
        original_metrics = [dict(row) for row in self.result["summary"]]
        self.assertEqual(self.root.title(), "SleepWell 연구자용 소프트웨어")
        self.app.var("title").set("User-authored title")
        self.app.var("annotation").set("User-authored note")
        self.app.navigate("results")
        self.app.change_language("en")
        self.assertEqual(self.app.page, "results")
        self.assertEqual(self.root.title(), "SleepWell Research Software")
        self.assertEqual(self.app.var("title").get(), "User-authored title")
        self.assertEqual(self.app.figure.axes[0].get_title(), "User-authored title")
        self.assertIn("User-authored note",
                      [text.get_text() for text in self.app.figure.texts])
        values = [self.app.result_tree.item(item, "values")
                  for item in self.app.result_tree.get_children()]
        self.assertIn("CMI · Physical fixed", [row[0] for row in values])
        self.app.change_language("ko")
        self.assertEqual(self.root.title(), "SleepWell 연구자용 소프트웨어")
        self.assertEqual(self.app.var("title").get(), "User-authored title")
        self.assertEqual(self.app.var("annotation").get(), "User-authored note")
        self.assertEqual(self.result["summary"], original_metrics)

    def test_current_language_is_used_for_figure_and_table_export(self):
        self.app.navigate("results")
        self.app.change_language("en")
        figure_path = Path(self.temp.name) / "paper.svg"
        table_path = Path(self.temp.name) / "results.xlsx"
        with mock.patch("researchbench.academic_app.filedialog.asksaveasfilename",
                        return_value=str(figure_path)):
            self.app.export_plot()
        with mock.patch("researchbench.academic_app.filedialog.asksaveasfilename",
                        return_value=str(table_path)):
            self.app.export_table()
        self.assertGreater(figure_path.stat().st_size, 1_000)
        with pd.ExcelFile(table_path) as workbook:
            self.assertIn("summary", workbook.sheet_names)
            self.assertIn("provenance", workbook.sheet_names)

    def test_report_specific_request_is_validated_and_forwarded(self):
        self.app.var("report_type").set("night_model")
        self.app.var("cohort").set("cmi")
        self.app.var("cutoff").set("2026-11-01")
        self.app.var("report_json").set('{"records": [], "research_context": true}')
        config = self.app.base_config("report")
        self.assertEqual(config["report_type"], "night_model")
        self.assertEqual(config["cohort"], "cmi")
        self.assertEqual(config["cutoff"], "2026-11-01T20:00:00+09:00")
        self.assertEqual(config["records"], [])
        self.assertTrue(config["research_context"])

        self.app.var("report_type").set("phone_baseline")
        self.app.var("report_json").set("{}")
        with self.assertRaises(ValueError):
            self.app.base_config("report")

        self.app.var("report_json").set('{"unsupported": 1}')
        with self.assertRaises(ValueError):
            self.app.base_config("report")


if __name__ == "__main__":
    unittest.main()
