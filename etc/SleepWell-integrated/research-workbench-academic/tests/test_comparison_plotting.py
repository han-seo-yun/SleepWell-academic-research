from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from researchbench.comparison_metrics import comparison_data
from researchbench.comparison_plotting import make_comparison_figure


class ComparisonPlottingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2] / "reports/simple_workflow_20261105/verified/workflow_smoke.swa/runs"
        if not root.is_dir():
            raise unittest.SkipTest("verified real SleepWell run is unavailable")
        record = json.loads(next(root.glob("*.json")).read_text(encoding="utf-8"))
        cls.record = record
        cls.data = comparison_data(record["result"], target="onset", tolerance=30)

    def test_default_metrics_match_preserved_participant_equal_engine(self):
        expected = {row["model_id"]: row for row in self.record["result"]["summary"]}
        for row in self.data["rows"]:
            with self.subTest(model=row["model_id"]):
                self.assertAlmostEqual(row["mae"], expected[row["model_id"]]["onset_mae_min"])
                self.assertAlmostEqual(row["rmse"], expected[row["model_id"]]["onset_rmse_min"])

    def test_all_seven_matplotlib_figures_render_real_nine_model_result(self):
        self.assertTrue(self.data["available"])
        self.assertEqual(len(self.data["model_ids"]), 9)
        with tempfile.TemporaryDirectory() as folder:
            for kind in ("mae", "rmse", "bias", "tolerance", "predictions", "errors", "participants"):
                with self.subTest(kind=kind):
                    figure = make_comparison_figure(self.data, kind=kind, lang="ko")
                    self.assertIsInstance(figure, Figure)
                    FigureCanvasAgg(figure).draw()
                    output = Path(folder) / f"{kind}.png"
                    figure.savefig(output, dpi=100, facecolor="white")
                    self.assertGreater(output.stat().st_size, 2_000)

    def test_shared_scatter_limits_and_regression_labels(self):
        figure = make_comparison_figure(self.data, kind="predictions", lang="en")
        visible = [axis for axis in figure.axes if axis.axison]
        self.assertEqual(len(visible), 9)
        self.assertTrue(all(axis.get_xlim() == visible[0].get_xlim() for axis in visible))
        self.assertTrue(all(axis.get_ylim() == visible[0].get_ylim() for axis in visible))
        self.assertNotIn("ROC", " ".join(axis.get_title() for axis in visible))

    def test_tolerance_rate_uses_fixed_percent_axis(self):
        data = dict(self.data)
        data["rows"] = [
            {**self.data["rows"][0], "within_tolerance": .2},
            {**self.data["rows"][1], "within_tolerance": .4},
        ]
        data["model_ids"] = [row["model_id"] for row in data["rows"]]
        figure = make_comparison_figure(data, kind="tolerance", lang="ko")
        self.assertEqual(figure.axes[0].get_xlim(), (0.0, 105.0))
        self.assertIn("±30분", figure.axes[0].get_title())

    def test_scatter_titles_and_footer_do_not_overlap_for_small_or_full_selection(self):
        for count in (1, 3, 9):
            data = comparison_data(self.record["result"], model_ids=self.data["model_ids"][:count])
            figure = make_comparison_figure(data, kind="predictions", lang="ko")
            canvas = FigureCanvasAgg(figure)
            canvas.draw()
            renderer = canvas.get_renderer()
            title_bottom = figure._suptitle.get_window_extent(renderer).y0
            footer_top = figure.texts[-1].get_window_extent(renderer).y1
            for axis in (axis for axis in figure.axes if axis.axison):
                self.assertLess(axis.title.get_window_extent(renderer).y1, title_bottom)
                self.assertGreater(axis.xaxis.label.get_window_extent(renderer).y0, footer_top)

    def test_footers_localize_and_describe_the_plotted_unit(self):
        predictions = make_comparison_figure(self.data, kind="predictions", lang="ko")
        self.assertIn("52밤, 3명", predictions.texts[-1].get_text())
        self.assertIn("야간 단위 점", predictions.texts[-1].get_text())
        rmse = make_comparison_figure(self.data, kind="rmse", lang="ko")
        self.assertIn("참여자별 RMSE의 평균", rmse.texts[-1].get_text())

    def test_participant_boxplot_overlays_actual_people(self):
        figure = make_comparison_figure(self.data, kind="participants", lang="ko")
        point_collections = figure.axes[0].collections
        self.assertEqual(len(point_collections), len(self.data["model_ids"]))
        self.assertTrue(all(len(collection.get_offsets()) == self.data["n_participants"]
                            for collection in point_collections))
        self.assertIn("참여자별 MAE", figure.texts[-1].get_text())

    def test_empty_comparison_has_explicit_matplotlib_figure(self):
        data = comparison_data({"raw": {"predictions": []}})
        figure = make_comparison_figure(data, kind="mae")
        self.assertIsInstance(figure, Figure)
        self.assertIn("비교 가능한", figure.axes[0].texts[0].get_text())


if __name__ == "__main__":
    unittest.main()
