from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.container import ErrorbarContainer
from matplotlib.figure import Figure

from researchbench.academic_backend import run_research
from researchbench.academic_plotting import (
    _observed_nights,
    export_academic_tables,
    export_figure,
    make_academic_figure,
    model_label,
)
from researchbench.academic_prospective import run_prospective


class AcademicPlottingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This calls the SHA-verified, byte-preserved research_lab implementation.
        cls.result = run_research({
            "mode": "personalization",
            "input_mode": "synthetic",
            "cohorts": ["cmi", "mesa"],
            "synthetic": {
                "n_nights": 12,
                "seed": 17,
                "start_date": "2026-01-01",
                "bed_hour": 23.5,
                "duration_hours": 8,
                "jitter_minutes": 30,
                "phase_shift_minutes": 45,
                "shift_after_night": 6,
                "missing_every": 0,
                "report_delay_hours": 1,
            },
        })

    def test_all_retrospective_figures_render_recorded_nine_arm_result(self):
        self.assertEqual(len(self.result["summary"]), 9)
        self.assertEqual(len({row["model_id"] for row in self.result["summary"]}), 9)
        with tempfile.TemporaryDirectory() as folder:
            for kind in ("comparison", "predictions", "correlation", "personalization", "errors", "source_report"):
                with self.subTest(kind=kind):
                    figure = make_academic_figure(self.result, kind=kind, lang="ko")
                    self.assertIsInstance(figure, Figure)
                    path = Path(folder) / f"{kind}.png"
                    export_figure(figure, path, dpi=140)
                    self.assertGreater(path.stat().st_size, 1_000)

        comparison = make_academic_figure(self.result, lang="ko")
        labels = [tick.get_text() for tick in comparison.axes[0].get_xticklabels()]
        self.assertIn("CMI · 수리 고정", labels)
        self.assertIn("MESA · 융합", labels)
        self.assertIn("최근 7일 기준", labels)
        self.assertFalse(any(isinstance(item, ErrorbarContainer)
                             for item in comparison.axes[0].containers))
        footer = [text for text in comparison.texts if "오차막대" in text.get_text()]
        self.assertEqual(len(footer), 1)
        self.assertIs(footer[0].get_transform(), comparison.transFigure)
        self.assertGreaterEqual(comparison.axes[0].get_position().y0, .33)
        thresholds = make_academic_figure(
            self.result, kind="personalization", lang="ko",
            options={"personalization_field": "sleep_threshold"},
        )
        self.assertTrue(thresholds.axes[0].lines)
        self.assertEqual(thresholds.axes[0].get_ylabel(), "수면 임계값 (무차원)")
        self.assertEqual(model_label("cmi.physical_fixed", "ko"), "CMI · 수리 고정")
        self.assertEqual(model_label("online_hybrid_mesa", "en"),
                         "MESA · Online personalized Fusion")
        self.assertEqual(model_label("physical_cmi", "ko"), "CMI · 수리")

    def test_observed_correlation_deduplicates_model_rows_by_night(self):
        predictions = [
            {"participant_id": "p1", "cutoff": "n1", "model_id": "a",
             "actual_onset_offset_min": 1, "actual_wake_offset_min": 10,
             "actual_interval_min": 9},
            {"participant_id": "p1", "cutoff": "n1", "model_id": "b",
             "actual_onset_offset_min": 1, "actual_wake_offset_min": 10,
             "actual_interval_min": 9},
            {"participant_id": "p1", "cutoff": "n1", "model_id": "c",
             "actual_onset_offset_min": 1, "actual_wake_offset_min": 10,
             "actual_interval_min": 9},
            {"participant_id": "p1", "cutoff": "n2", "model_id": "a",
             "actual_onset_offset_min": 2, "actual_wake_offset_min": 14,
             "actual_interval_min": 12},
            {"participant_id": "p2", "cutoff": "n3", "model_id": "a",
             "actual_onset_offset_min": 5, "actual_wake_offset_min": 15,
             "actual_interval_min": 10},
        ]
        result = {"mode": "retrospective", "raw": {"predictions": predictions}}
        observed = _observed_nights(result)
        self.assertEqual(len(observed), 3)
        self.assertEqual(observed[["participant_id", "cutoff"]].values.tolist(),
                         [["p1", "n1"], ["p1", "n2"], ["p2", "n3"]])

        figure = make_academic_figure(
            result, kind="correlation", lang="en", options={"corr_method": "spearman"}
        )
        expected = observed[[
            "actual_onset_offset_min", "actual_wake_offset_min", "actual_interval_min"
        ]].corr(method="spearman").to_numpy()
        np.testing.assert_allclose(figure.axes[0].images[0].get_array(), expected)
        footer = "\n".join(text.get_text() for text in figure.texts)
        self.assertIn("deduplicated by participant and cutoff, n=3 nights", footer)
        self.assertIn("mathematically derived", footer)

    def test_personalization_plot_uses_recorded_engine_state_values(self):
        rows = self.result["raw"]["personalization"]
        expected: dict[tuple[str, str], list[float]] = {}
        for row in rows:
            key = (str(row["participant_id"]), str(row["cohort"]))
            expected.setdefault(key, []).append(float(row["state"]["phase_min"]))
        figure = make_academic_figure(
            self.result, kind="personalization", lang="en",
            options={"personalization_field": "phase_min"},
        )
        self.assertEqual(figure.axes[0].get_ylabel(), "Behavior-adjusted phase (min)")
        plotted = {
            tuple(line.get_label().replace(" · ", "|").lower().split("|")):
                line.get_ydata().tolist()
            for line in figure.axes[0].lines
        }
        normalized_expected = {(participant.lower(), cohort.lower()): values
                               for (participant, cohort), values in expected.items()}
        self.assertEqual(set(plotted), set(normalized_expected))
        for key, values in normalized_expected.items():
            np.testing.assert_allclose(plotted[key], values, rtol=0, atol=1e-12)

    def test_single_night_error_is_scatter_only_and_labeled_as_night_level(self):
        result = {"mode": "retrospective", "raw": {"predictions": [{
            "participant_id": "p1", "cutoff": "n1", "model_id": "only",
            "actual_onset_offset_min": 10, "onset_offset_min": 12,
            "actual_wake_offset_min": 20, "wake_offset_min": 23,
        }]}}
        figure = make_academic_figure(result, kind="errors", lang="en")
        self.assertFalse(figure.axes[0].lines)
        self.assertTrue(figure.axes[0].collections)
        footer = "\n".join(text.get_text() for text in figure.texts)
        self.assertIn("night-level observations, not cross-validation folds", footer)
        self.assertIn("n=1 model-night results", footer)

    def test_short_wide_canvas_keeps_axis_labels_clear_of_english_footers(self):
        for kind in ("comparison", "correlation"):
            with self.subTest(kind=kind):
                figure = make_academic_figure(
                    self.result, kind=kind, lang="en",
                    options={"width": 12.3, "height": 5.5, "dpi": 100},
                )
                canvas = FigureCanvasAgg(figure)
                canvas.draw()
                renderer = canvas.get_renderer()
                footer_top = max(text.get_window_extent(renderer).y1
                                 for text in figure.texts)
                axis = figure.axes[0]
                lower_edges = [
                    label.get_window_extent(renderer).y0
                    for label in axis.get_xticklabels()
                    if label.get_visible() and label.get_text()
                ]
                xlabel_box = axis.xaxis.label.get_window_extent(renderer)
                if xlabel_box.height:
                    lower_edges.append(xlabel_box.y0)
                self.assertGreater(min(lower_edges) - footer_top, 20)
                if kind == "correlation":
                    ytick_left = min(
                        label.get_window_extent(renderer).x0
                        for label in axis.get_yticklabels()
                        if label.get_visible() and label.get_text()
                    )
                    self.assertGreaterEqual(ytick_left, 0)

    def test_wrapped_run_custom_text_and_language_do_not_change_values(self):
        wrapped = {"id": "academic-1", "name": "원본 실험명", "result": self.result}
        figure = make_academic_figure(
            wrapped, lang="en",
            options={"title": "Author title", "annotation": "Author note",
                     "xlabel": "Model arm", "ylabel": "Recorded minutes"},
        )
        axis = figure.axes[0]
        self.assertEqual(axis.get_title(), "Author title")
        self.assertEqual(axis.get_xlabel(), "Model arm")
        self.assertEqual(axis.get_ylabel(), "Recorded minutes")
        self.assertIn("Author note", [text.get_text() for text in figure.texts])
        # Shared table/figure order is now ascending, while recorded model values stay unchanged.
        ordered = sorted(self.result['summary'],key=lambda row:row['joint_mae_min'])
        expected = [row["joint_mae_min"] for row in ordered]
        actual = [bar.get_height() for bar in axis.patches]
        self.assertEqual(actual, expected)
        self.assertEqual([label.get_text() for label in axis.get_xticklabels()],
                         [model_label(row['model_id'],'en') for row in ordered])

    def test_xlsx_and_csv_exports_preserve_metrics_predictions_and_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            workbook = Path(folder) / "학술제_결과.xlsx"
            export_academic_tables(self.result, workbook, lang="ko")
            with pd.ExcelFile(workbook) as excel:
                sheets = excel.sheet_names
                self.assertIn("성능 요약", sheets)
                self.assertIn("예측값", sheets)
                self.assertIn("출처 정보", sheets)
                self.assertIn("실행 메타데이터", sheets)
            summary = pd.read_excel(workbook, sheet_name="성능 요약")
            predictions = pd.read_excel(workbook, sheet_name="예측값")
            provenance = pd.read_excel(workbook, sheet_name="출처 정보")
            self.assertEqual(summary["model_id"].tolist(),
                             [row["model_id"] for row in self.result["summary"]])
            np.testing.assert_allclose(
                summary["joint_mae_min"].to_numpy(),
                [row["joint_mae_min"] for row in self.result["summary"]],
                rtol=0, atol=1e-12,
            )
            self.assertEqual(len(predictions), len(self.result["raw"]["predictions"]))
            self.assertEqual(provenance.loc[0, "source_policy"], "exact-byte-copy")

            csv_path = Path(folder) / "academic.csv"
            export_academic_tables(self.result, csv_path, lang="en")
            self.assertEqual(pd.read_csv(csv_path)["model_id"].tolist(),
                             [row["model_id"] for row in self.result["summary"]])
            self.assertTrue((Path(folder) / "academic_predictions.csv").is_file())

    def test_prospective_demo_flattens_actual_forecasts_and_online_weights(self):
        result = run_prospective({"mode": "prospective_demo", "n_nights": 4, "seed": 9})
        predicted = make_academic_figure(result, kind="predictions", lang="ko")
        personalized = make_academic_figure(result, kind="personalization", lang="ko")
        errors = make_academic_figure(result, kind="errors", lang="ko")
        self.assertTrue(predicted.axes[0].collections)
        self.assertTrue(personalized.axes[0].lines)
        self.assertTrue(errors.axes[0].lines)
        labels = [item.get_text() for item in predicted.axes[0].get_legend().get_texts()]
        self.assertTrue(any("온라인 개인화" in label for label in labels))

    def test_source_report_uses_normalized_original_table_when_no_mae_exists(self):
        report = run_research({
            "mode": "report", "report_type": "population_prior",
            "profile": {"age": 50, "sex": "female"},
        })
        self.assertFalse(report["summary"])
        figure = make_academic_figure(report, kind="source_report", lang="en")
        self.assertTrue(figure.axes[0].tables)
        self.assertFalse(figure.axes[0].axison)

    def test_missing_metric_is_not_drawn_as_zero(self):
        unavailable = {
            "mode": "report", "raw": {},
            "summary": [{"model_id": "unavailable", "joint_mae_min": None}],
            "provenance": {}, "warnings": ["not calculable"],
        }
        figure = make_academic_figure(unavailable, lang="ko")
        self.assertFalse(figure.axes[0].patches)
        self.assertIn("표시할 계산 결과가 없습니다.",
                      [text.get_text() for text in figure.axes[0].texts])

    def test_synthetic_and_stored_evidence_captions_are_unambiguous(self):
        synthetic = dict(self.result)
        synthetic["provenance"] = dict(synthetic["provenance"], input_synthetic=True)
        figure = make_academic_figure(
            synthetic, lang="ko", options={"annotation": "사용자 주석"}
        )
        footer = "\n".join(text.get_text() for text in figure.texts)
        self.assertIn("사용자 주석", footer)
        self.assertIn("합성 시연 · 실제 연구 성능 아님", footer)
        self.assertNotIn("저장된 과거 검증 근거", footer)

        stored = run_research({
            "mode": "report", "report_type": "target_validity_validation_v1"
        })
        stored_figure = make_academic_figure(stored, kind="source_report", lang="en")
        stored_footer = "\n".join(text.get_text() for text in stored_figure.texts)
        self.assertIn("Stored historical validation evidence", stored_footer)
        self.assertNotIn("Synthetic demonstration", stored_footer)


if __name__ == "__main__":
    unittest.main()
