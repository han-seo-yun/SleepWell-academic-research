import base64
import copy
from datetime import date, timedelta
import inspect
from io import BytesIO
import json
import re
import struct
import unittest
from unittest.mock import patch
import zipfile

from matplotlib._pylab_helpers import Gcf
from matplotlib.axes import Axes

import research_lab_charts as chart_module
from research_lab_charts import export_figures, render_charts


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MODEL_IDS = (
    "baseline.recent7",
    "cmi.physical_fixed",
    "cmi.physical_personal",
    "cmi.data",
    "cmi.hybrid",
    "mesa.physical_fixed",
    "mesa.physical_personal",
    "mesa.data",
    "mesa.hybrid",
)


def _model(model_id):
    if model_id == "baseline.recent7":
        return {
            "id": model_id,
            "label": "Recent 7-night baseline",
            "cohort": None,
            "kind": "baseline_recent7",
            "artifact_sha256": None,
        }
    cohort, kind = model_id.split(".", 1)
    return {
        "id": model_id,
        "label": kind.replace("_", " ").title(),
        "cohort": cohort,
        "kind": kind,
        "artifact_sha256": ("a" if cohort == "cmi" else "b") * 64,
    }


def sample_result():
    cutoffs = [f"2026-01-{day:02d}T20:00:00+09:00" for day in range(1, 5)]
    predictions = []
    for night_index, cutoff in enumerate(cutoffs):
        actual_onset = None if night_index == 1 else 215.0 + night_index * 8.0
        actual_wake = None if night_index == 1 else 660.0 + night_index * 5.0
        for model_index, model_id in enumerate(MODEL_IDS):
            predictions.append(
                {
                    "participant_id": "P-001",
                    "cutoff": cutoff,
                    "model_id": model_id,
                    "onset_offset_min": 210.0 + night_index * 7.0 + model_index,
                    "wake_offset_min": 650.0 + night_index * 6.0 + model_index,
                    "interval_min": 440.0 + model_index,
                    "actual_onset_offset_min": actual_onset,
                    "actual_wake_offset_min": actual_wake,
                    "actual_interval_min": None if actual_onset is None else actual_wake - actual_onset,
                }
            )

    metrics = []
    for index, model_id in enumerate(MODEL_IDS):
        metrics.append(
            {
                "model_id": model_id,
                "n": 3,
                "n_participants": 1,
                "averaging": "participant_equal",
                "onset_mae_min": 18.0 + index,
                "wake_mae_min": 22.0 + index,
                "interval_mae_min": 25.0 + index,
                "joint_mae_min": 20.0 + index,
                "onset_rmse_min": 21.0 + index,
                "wake_rmse_min": 24.0 + index,
                "skill_vs_recent7": None if index == 0 else 0.1,
            }
        )

    personalization = []
    for cohort_index, cohort in enumerate(("cmi", "mesa")):
        for night_index, cutoff in enumerate(cutoffs):
            personalization.append(
                {
                    "participant_id": "P-001",
                    "cutoff": cutoff,
                    "cohort": cohort,
                    "history_count": night_index,
                    "state": {
                        "personal_weight": 0.1 * night_index,
                        "phase_min": -12.0 + night_index * 4.0 + cohort_index,
                        "sleep_threshold": 0.68 + night_index * 0.01,
                        "wake_threshold": 0.28 + night_index * 0.01,
                        "tau_wake_h": 18.2,
                        "tau_sleep_h": 4.2,
                    },
                    "features": {
                        "current_s": 0.55 + night_index * 0.02,
                        "current_c": -0.1 + night_index * 0.04 + cohort_index * 0.01,
                        "history_count_28d": night_index,
                    },
                }
            )

    return {
        "schema_version": 1,
        "mode": "synthetic",
        "evidence_label": "seeded synthetic demo; not real accuracy evidence",
        "run_id": "test-run",
        "elapsed_ms": 12.5,
        "summary": {
            "participants": ["P-001"],
            "rows": 4,
            "labeled_nights": 3,
            "common_nights": 3,
            "chart_participant_id": "P-001",
        },
        "models": [_model(model_id) for model_id in MODEL_IDS],
        "metrics": metrics,
        "available_metrics": copy.deepcopy(metrics),
        "predictions": predictions,
        "personalization": personalization,
        "input_contract": {"cutoff": "20:00:00+09:00"},
        "warnings": [],
    }


def decode_png(chart):
    prefix = "data:image/png;base64,"
    if not chart["data_uri"].startswith(prefix):
        raise AssertionError("chart is not a PNG data URI")
    raw = base64.b64decode(chart["data_uri"][len(prefix) :], validate=True)
    if raw[:8] != PNG_SIGNATURE or raw[12:16] != b"IHDR":
        raise AssertionError("invalid PNG header")
    width, height = struct.unpack(">II", raw[16:24])
    return raw, width, height


class ResearchLabChartTests(unittest.TestCase):
    def test_participant_equal_and_paired_estimands_do_not_pool_uneven_nights(self):
        result = sample_result()
        result["models"] = [_model("baseline.recent7"), _model("cmi.hybrid")]
        result["predictions"] = []
        for participant_id, errors in (("P-one-night", [0.0]), ("P-three-nights", [10.0, 10.0, 10.0])):
            for night_index, baseline_error in enumerate(errors):
                cutoff = f"2026-03-{night_index + 1:02d}T20:00:00+09:00"
                for model_id, error in (("baseline.recent7", baseline_error), ("cmi.hybrid", baseline_error + 5.0)):
                    result["predictions"].append(
                        {
                            "participant_id": participant_id, "cutoff": cutoff, "model_id": model_id,
                            "onset_offset_min": 200.0 + error, "wake_offset_min": 600.0 + error,
                            "actual_onset_offset_min": 200.0, "actual_wake_offset_min": 600.0,
                        }
                    )

        diagnostic = chart_module._common_diagnostic_data(result)
        baseline = chart_module._participant_joint_error_map(diagnostic, "baseline.recent7")
        fusion = chart_module._participant_joint_error_map(diagnostic, "cmi.hybrid")
        self.assertEqual(baseline, {"P-one-night": 0.0, "P-three-nights": 10.0})
        self.assertEqual(sum(baseline.values()) / len(baseline), 5.0)
        self.assertEqual([fusion[key] - baseline[key] for key in sorted(baseline)], [5.0, 5.0])
        self.assertEqual(chart_module._bootstrap_mean_ci([5.0, 5.0], "paired-test"), (5.0, 5.0))
        self.assertIsNone(chart_module._bootstrap_mean_ci([5.0], "n-one"))

    def test_model_colors_are_stable_when_a_panel_excludes_baseline(self):
        result = sample_result()
        models, _order = chart_module._model_maps(result)
        for model_id in MODEL_IDS:
            self.assertEqual(
                chart_module._model_color(model_id, 0, models),
                chart_module._model_color(model_id, 15, models),
            )

    def test_png_descriptors_dimensions_and_synthetic_evidence(self):
        charts = render_charts(sample_result())
        self.assertEqual(
            [chart["id"] for chart in charts],
            [
                "prediction-timeline",
                "model-mae-comparison",
                "personalization-traces",
                "participant-mae-heatmap",
                "joint-error-distribution",
                "signed-residual-bias",
                "sleep-actogram",
                "data-completeness",
            ],
        )
        self.assertEqual(
            [chart["title"] for chart in charts],
            [
                "관측·예측 수면 시각",
                "동일 밤 모델 MAE 비교",
                "개인화 상태·모델 입력 추적",
                "모델·참가자 공통 밤 MAE",
                "참가자별 joint MAE 분포",
                "signed onset·wake 잔차",
                "이중 플롯 수면 액토그램",
                "데이터 완결성·입력 시점",
            ],
        )
        for chart in charts:
            self.assertEqual(set(chart), {"id", "title", "alt", "data_uri"})
            self.assertIn("SYNTHETIC DEMONSTRATION", chart["alt"])
            self.assertIn("not real accuracy evidence", chart["alt"])
            raw, width, height = decode_png(chart)
            self.assertEqual(raw[:8], PNG_SIGNATURE)
            self.assertGreaterEqual(width, 600)
            self.assertGreaterEqual(height, 500)
            self.assertLessEqual(width, 1600)
            self.assertLessEqual(height, 1200)
            self.assertIn(b"SYNTHETIC DEMONSTRATION", raw)

    def test_missing_outcomes_has_explanatory_panel_and_no_fake_bars(self):
        result = sample_result()
        result["summary"]["labeled_nights"] = 0
        result["summary"]["common_nights"] = 0
        result["metrics"] = []
        for prediction in result["predictions"]:
            prediction["actual_onset_offset_min"] = None
            prediction["actual_wake_offset_min"] = None
            prediction["actual_interval_min"] = None

        calls = []
        original_barh = Axes.barh

        def tracked_barh(axis, *args, **kwargs):
            calls.append((args, kwargs))
            return original_barh(axis, *args, **kwargs)

        with (
            patch.object(Axes, "barh", new=tracked_barh),
            patch.object(Axes, "imshow") as imshow,
            patch.object(Axes, "boxplot") as boxplot,
            patch.object(Axes, "scatter") as scatter,
        ):
            charts = render_charts(result)

        comparison = next(chart for chart in charts if chart["id"] == "model-mae-comparison")
        timeline = next(chart for chart in charts if chart["id"] == "prediction-timeline")
        diagnostics = [
            chart
            for chart in charts
            if chart["id"]
            in {"participant-mae-heatmap", "joint-error-distribution", "signed-residual-bias"}
        ]
        self.assertIn("Accuracy unavailable", comparison["alt"])
        self.assertIn("가짜 0 막대", comparison["alt"])
        self.assertIn("예측선만", timeline["alt"])
        self.assertEqual(len(diagnostics), 3)
        for chart in diagnostics:
            self.assertIn("Accuracy unavailable", chart["alt"])
        self.assertEqual(calls, [])
        imshow.assert_not_called()
        boxplot.assert_not_called()
        scatter.assert_not_called()
        raw, _, _ = decode_png(comparison)
        self.assertIn(b"Accuracy unavailable", raw)

    def test_zero_metrics_and_constant_or_empty_states_render(self):
        result = sample_result()
        for metric in result["metrics"]:
            for key in ("onset_mae_min", "wake_mae_min", "interval_mae_min", "joint_mae_min"):
                metric[key] = 0.0
        for row in result["personalization"]:
            row["history_count"] = 0
            row["state"].update(
                personal_weight=0.0,
                phase_min=0.0,
                sleep_threshold=0.0,
                wake_threshold=0.0,
            )
            row["features"].update(current_s=0.0, current_c=0.0)

        point_calls = []
        original_scatter = Axes.scatter

        def tracked_scatter(axis, *args, **kwargs):
            point_calls.append((args, kwargs))
            return original_scatter(axis, *args, **kwargs)

        with patch.object(Axes, "scatter", new=tracked_scatter):
            constant_charts = render_charts(result)
        self.assertTrue(point_calls, "finite zero MAEs must be rendered, not treated as missing")
        comparison = next(chart for chart in constant_charts if chart["id"] == "model-mae-comparison")
        self.assertNotIn("Accuracy unavailable", comparison["alt"])
        for chart in constant_charts:
            decode_png(chart)

        result["predictions"] = []
        result["personalization"] = []
        result["metrics"] = []
        empty_charts = render_charts(result)
        self.assertIn("표시할 예측 행이 없습니다", empty_charts[0]["alt"])
        self.assertIn("상태 스냅샷이 없습니다", empty_charts[2]["alt"])
        for chart in empty_charts:
            decode_png(chart)

    def test_diagnostics_use_common_nights_and_state_aggregation_semantics(self):
        result = sample_result()
        model_ids = ("baseline.recent7", "cmi.hybrid")
        result["models"] = [_model(model_id) for model_id in model_ids]
        result["predictions"] = []
        result["metrics"] = []
        result["available_metrics"] = []
        result["personalization"] = []
        result["summary"]["participants"] = [f"P-{index:03d}" for index in range(1, 13)]
        for participant_index in range(1, 13):
            participant_id = f"P-{participant_index:03d}"
            for night_index in range(2):
                cutoff = f"2026-02-{night_index + 1:02d}T20:00:00+09:00"
                actual_onset = 220.0 + participant_index
                actual_wake = 660.0 + participant_index
                for model_index, model_id in enumerate(model_ids):
                    if participant_index == 12 and night_index == 1 and model_id == "cmi.hybrid":
                        continue
                    result["predictions"].append(
                        {
                            "participant_id": participant_id,
                            "cutoff": cutoff,
                            "model_id": model_id,
                            "onset_offset_min": actual_onset + participant_index + model_index,
                            "wake_offset_min": actual_wake - participant_index + model_index,
                            "interval_min": 440.0,
                            "actual_onset_offset_min": actual_onset,
                            "actual_wake_offset_min": actual_wake,
                            "actual_interval_min": 440.0,
                        }
                    )

        imshow_shapes = []
        boxplot_lengths = []
        scatter_lengths = []
        original_imshow = Axes.imshow
        original_boxplot = Axes.boxplot
        original_scatter = Axes.scatter

        def tracked_imshow(axis, values, *args, **kwargs):
            imshow_shapes.append((len(values), len(values[0])))
            return original_imshow(axis, values, *args, **kwargs)

        def tracked_boxplot(axis, values, *args, tick_labels=None, labels=None, **kwargs):
            boxplot_lengths.append([len(distribution) for distribution in values])
            label_values = tick_labels if tick_labels is not None else labels
            if label_values is not None:
                label_keyword = (
                    "tick_labels"
                    if "tick_labels" in inspect.signature(original_boxplot).parameters
                    else "labels"
                )
                kwargs[label_keyword] = label_values
            return original_boxplot(axis, values, *args, **kwargs)

        def tracked_scatter(axis, x, y, *args, **kwargs):
            scatter_lengths.append((len(x), len(y), kwargs.get("marker")))
            return original_scatter(axis, x, y, *args, **kwargs)

        with (
            patch.object(Axes, "imshow", new=tracked_imshow),
            patch.object(Axes, "boxplot", new=tracked_boxplot),
            patch.object(Axes, "scatter", new=tracked_scatter),
        ):
            charts = render_charts(result)

        heatmap = next(chart for chart in charts if chart["id"] == "participant-mae-heatmap")
        distribution = next(chart for chart in charts if chart["id"] == "joint-error-distribution")
        residual = next(chart for chart in charts if chart["id"] == "signed-residual-bias")
        self.assertEqual(imshow_shapes, [(2, 10)])
        self.assertEqual(boxplot_lengths, [[12, 12]])
        self.assertEqual(
            [length for length in scatter_lengths if length[2] != "*"],
            [(12, 12, None)] * 2 + [(10, 10, None)] * 2,
        )
        self.assertEqual([length for length in scatter_lengths if length[2] == "*"], [(1, 1, "*")] * 2)
        self.assertIn("10/12명", heatmap["alt"])
        self.assertIn("공통 평가 밤 23개", heatmap["alt"])
        self.assertIn("점 하나는 참가자 한 명", distribution["alt"])
        self.assertIn("참가자 n=12", distribution["alt"])
        self.assertIn("별은 모델별 participant_equal 평균", residual["alt"])
        for chart in charts:
            decode_png(chart)

    def test_result_is_not_mutated_and_figures_are_not_registered(self):
        result = sample_result()
        original = copy.deepcopy(result)
        managers_before = tuple(manager.num for manager in Gcf.get_all_fig_managers())

        first = render_charts(result)
        second = render_charts(result)

        self.assertEqual(result, original)
        self.assertEqual(tuple(manager.num for manager in Gcf.get_all_fig_managers()), managers_before)
        self.assertEqual([decode_png(chart)[1:] for chart in first], [decode_png(chart)[1:] for chart in second])

    def test_each_plotted_series_is_bounded_to_365_points(self):
        result = sample_result()
        result["models"] = [_model("baseline.recent7"), _model("cmi.hybrid")]
        result["metrics"] = result["metrics"][:1]
        result["available_metrics"] = copy.deepcopy(result["metrics"])
        result["predictions"] = []
        result["personalization"] = []
        start = date(2025, 1, 1)
        for index in range(390):
            cutoff = f"{start + timedelta(days=index)}T20:00:00+09:00"
            for model_id in ("baseline.recent7", "cmi.hybrid"):
                result["predictions"].append(
                    {
                        "participant_id": "P-001",
                        "cutoff": cutoff,
                        "model_id": model_id,
                        "onset_offset_min": 220.0,
                        "wake_offset_min": 660.0,
                        "interval_min": 440.0,
                        "actual_onset_offset_min": 225.0,
                        "actual_wake_offset_min": 665.0,
                        "actual_interval_min": 440.0,
                    }
                )
            result["personalization"].append(
                {
                    "participant_id": "P-001",
                    "cutoff": cutoff,
                    "cohort": "cmi",
                    "history_count": index,
                    "state": {
                        "personal_weight": 0.5,
                        "phase_min": 0.0,
                        "sleep_threshold": 0.7,
                        "wake_threshold": 0.3,
                        "tau_wake_h": 18.2,
                        "tau_sleep_h": 4.2,
                    },
                    "features": {"current_s": 0.6, "current_c": 0.0},
                }
            )

        lengths = []
        original_plot = Axes.plot

        def tracked_plot(axis, *args, **kwargs):
            if args and hasattr(args[0], "__len__"):
                lengths.append(len(args[0]))
            return original_plot(axis, *args, **kwargs)

        with patch.object(Axes, "plot", new=tracked_plot):
            charts = render_charts(result)
        self.assertTrue(lengths)
        self.assertLessEqual(max(lengths), 365)
        heatmap = next(chart for chart in charts if chart["id"] == "participant-mae-heatmap")
        distribution = next(chart for chart in charts if chart["id"] == "joint-error-distribution")
        self.assertIn("공통 평가 밤 365/390개", heatmap["alt"])
        self.assertIn("사용한 공통 밤 365/390개", distribution["alt"])
        for chart in charts:
            decode_png(chart)

    def test_publication_bundle_has_vector_raster_source_and_reproduction_files(self):
        result = sample_result()
        original = copy.deepcopy(result)
        payload = export_figures(result)
        self.assertEqual(result, original)

        with zipfile.ZipFile(BytesIO(payload)) as bundle:
            names = set(bundle.namelist())
            self.assertIn("figure-manifest.json", names)
            self.assertIn("source-data.csv", names)
            self.assertIn("result.json", names)
            self.assertIn("reproduce.py", names)
            self.assertIn("research_lab_charts.py", names)
            manifest = json.loads(bundle.read("figure-manifest.json"))
            self.assertEqual(manifest["figure_count"], 8)
            self.assertEqual(len(manifest["figures"]), 8)
            self.assertTrue(all(item["caption"] for item in manifest["figures"]))
            self.assertTrue(all(item["text_bounds_checked"] for item in manifest["figures"]))
            self.assertTrue(all(item["source_fields"] for item in manifest["figures"]))
            heatmap_record = next(item for item in manifest["figures"] if item["id"] == "participant-mae-heatmap")
            self.assertEqual(heatmap_record["display_participant_mapping"], {"P01": "P-001"})
            self.assertEqual(manifest["status"], "provisional_general_publication_export")
            self.assertIn("participant bootstrap", manifest["estimands"]["uncertainty"])
            self.assertEqual(json.loads(bundle.read("result.json")), result)
            source_data = bundle.read("source-data.csv")
            self.assertIn(b"participant_equal_metric", source_data)
            self.assertIn(b"display_participant", source_data)
            self.assertIn(b"interval_mae_min", source_data)
            self.assertIn(b"remain absent/gaps", bundle.read("README.txt"))

            for index, chart_id in enumerate(
                (
                    "prediction-timeline", "model-mae-comparison", "personalization-traces",
                    "participant-mae-heatmap", "joint-error-distribution", "signed-residual-bias",
                    "sleep-actogram", "data-completeness",
                ),
                start=1,
            ):
                stem = f"figures/{index:02d}-{chart_id}"
                png = bundle.read(stem + ".png")
                pdf = bundle.read(stem + ".pdf")
                svg = bundle.read(stem + ".svg")
                self.assertEqual(png[:8], PNG_SIGNATURE)
                width, height = struct.unpack(">II", png[16:24])
                self.assertIn(width, range(2124, 2128))
                self.assertGreater(height, 1200)
                self.assertTrue(pdf.startswith(b"%PDF-"))
                self.assertIn(b"<svg", svg[:2000])
                self.assertIn("SYNTHETIC DATA", svg.decode("utf-8"))

            pdf = bundle.read("figures/01-prediction-timeline.pdf")
            media_box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([0-9.]+)\s+([0-9.]+)\s*\]", pdf)
            self.assertIsNotNone(media_box)
            self.assertAlmostEqual(float(media_box.group(1)), 180 / 25.4 * 72, delta=0.5)


if __name__ == "__main__":
    unittest.main()
