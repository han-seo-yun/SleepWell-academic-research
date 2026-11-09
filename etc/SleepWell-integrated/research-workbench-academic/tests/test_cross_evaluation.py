from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from researchbench import cross_evaluation as cross


class _RecordingTransform:
    def __init__(self, shift: float):
        self.shift = shift
        self.seen = None

    def transform(self, values):
        self.seen = np.asarray(values, float).copy()
        return self.seen + self.shift


class _EndpointEstimator:
    def predict(self, values):
        values = np.asarray(values, float)
        return np.column_stack([values[:, 0], values[:, 0] + 120.0])


def evaluation_frame() -> pd.DataFrame:
    return pd.DataFrame([
        {"cohort": "CMI", "subject_id": "c1", "split": "test",
         "bedtime_min": 100.0, "wake_min": 500.0},
        {"cohort": "MESA", "subject_id": "m1", "split": "test",
         "bedtime_min": 200.0, "wake_min": 600.0},
    ])


class CrossEvaluationTests(unittest.TestCase):
    def test_source_predictor_applies_one_declared_source_to_every_target_row(self):
        transform = _RecordingTransform(10.0)
        bundle = {
            "strategy": "separate",
            "models": {"CMI": _EndpointEstimator(), "MESA": object()},
            "preprocessors": {"CMI": transform, "MESA": object()},
            "features": ["feature"],
            "source_encoding": [],
        }
        features = pd.DataFrame({"feature": [1.0, 9.0]}, index=["cmi-row", "mesa-row"])

        predicted = cross.predict_source(bundle, "CMI", features)

        np.testing.assert_array_equal(transform.seen, [[1.0], [9.0]])
        np.testing.assert_array_equal(predicted, [[11.0, 131.0], [19.0, 139.0]])

    def test_endpoint_mean_is_arithmetic_then_projected_to_common_support(self):
        left = np.array([[100.0, 130.0], [900.0, 1000.0]])
        right = np.array([[300.0, 100.0], [1100.0, 1600.0]])

        predicted = cross.mean_predictions(left, right)

        # Row one needs the wake >= onset+30 projection after endpoint averaging.
        # Row two needs both the onset ceiling and wake ceiling.
        np.testing.assert_array_equal(predicted, [[200.0, 230.0], [960.0, 1300.0]])

    def test_replay_excludes_late_observation_and_uses_prior_at_cold_start(self):
        frame = pd.DataFrame([
            {"cohort": "CMI", "subject_id": "p1", "split": "test",
             "cutoff": "2026-01-01T20:00:00+09:00",
             "bed": "2026-01-01T23:00:00+09:00", "wake": "2026-01-02T07:00:00+09:00",
             "observed_at": "2030-01-01T08:00:00+09:00"},
            {"cohort": "CMI", "subject_id": "p1", "split": "test",
             "cutoff": "2026-01-02T20:00:00+09:00",
             "bed": "2026-01-02T22:30:00+09:00", "wake": "2026-01-03T06:30:00+09:00",
             "observed_at": "2026-01-03T07:00:00+09:00"},
            {"cohort": "CMI", "subject_id": "p1", "split": "test",
             "cutoff": "2026-01-03T20:00:00+09:00",
             "bed": "2026-01-03T22:00:00+09:00", "wake": "2026-01-04T06:00:00+09:00",
             "observed_at": "2026-01-04T07:00:00+09:00"},
        ])
        prior = {"bed_offset": 170.0, "wake_offset": 650.0}

        features, _, counts = cross.pt.replay_features(frame, prior)

        self.assertEqual(counts, [0, 0, 1])
        self.assertEqual(features.recent_count14.tolist(), [0.0, 0.0, 1.0])
        self.assertEqual(features.loc[0, "median14_bed"], prior["bed_offset"])
        self.assertEqual(features.loc[0, "median14_wake"], prior["wake_offset"])
        # The second night's record becomes available before the third cutoff.
        self.assertEqual(features.loc[2, "median14_bed"], 150.0)
        self.assertEqual(features.loc[2, "median14_wake"], 630.0)

    def test_history_summary_omits_macro_when_a_band_has_only_one_cohort(self):
        frame = evaluation_frame()
        counts = [0, 1]
        baseline = np.array([[100.0, 500.0], [200.0, 600.0]])
        predictions = {name: baseline.copy() for name in cross.PRIMARY_MODELS}

        rows, differences = cross.history_summaries(
            frame, predictions, counts, replicates=100, seed=17,
        )

        self.assertEqual({(row["history_band"], row["cohort"]) for row in rows},
                         {("0", "CMI"), ("1-3", "MESA")})
        self.assertNotIn("MACRO", {row["cohort"] for row in rows})
        self.assertNotIn("MACRO", {row["cohort"] for row in differences})

    def test_history_band_boundaries_and_negative_count_rejection(self):
        expected = {
            0: "0", 1: "1-3", 3: "1-3", 4: "4-6", 6: "4-6",
            7: "7-13", 13: "7-13", 14: "14+", 50: "14+",
        }
        self.assertEqual({value: cross.history_band(value) for value in expected}, expected)
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            cross.history_band(-1)

    def test_paired_comparison_rejects_differently_ordered_participants(self):
        left_index = pd.MultiIndex.from_tuples(
            [("CMI", "c1"), ("MESA", "m1")], names=["cohort", "subject_id"])
        right_index = pd.MultiIndex.from_tuples(
            [("MESA", "m1"), ("CMI", "c1")], names=["cohort", "subject_id"])
        errors = {
            "left": pd.DataFrame({"joint_mae_min": [1.0, 2.0]}, index=left_index),
            "right": pd.DataFrame({"joint_mae_min": [2.0, 1.0]}, index=right_index),
        }

        with self.assertRaisesRegex(ValueError, "alignment"):
            cross.paired_rows(errors, {}, [("left", "right")])

    def test_paired_macro_uses_equal_cohort_average_with_metric_summary_bootstraps(self):
        frame = pd.DataFrame([
            {"cohort": "CMI", "subject_id": "c1", "bedtime_min": 100.0, "wake_min": 500.0},
            {"cohort": "CMI", "subject_id": "c1", "bedtime_min": 110.0, "wake_min": 510.0},
            {"cohort": "CMI", "subject_id": "c2", "bedtime_min": 120.0, "wake_min": 520.0},
            {"cohort": "MESA", "subject_id": "m1", "bedtime_min": 200.0, "wake_min": 600.0},
            {"cohort": "MESA", "subject_id": "m1", "bedtime_min": 210.0, "wake_min": 610.0},
            {"cohort": "MESA", "subject_id": "m1", "bedtime_min": 220.0, "wake_min": 620.0},
        ])
        actual = frame[["bedtime_min", "wake_min"]].to_numpy(float)
        reference = actual + np.array([
            [10, 10], [30, 30], [40, 40], [20, 20], [40, 40], [60, 60],
        ])
        model = actual + np.array([
            [5, 5], [15, 15], [10, 10], [10, 10], [20, 20], [30, 30],
        ])

        _, errors, bootstraps = cross.pt.metric_summaries(
            frame, {"reference": reference, "model": model},
            replicates=100, seed=23, split="test",
        )
        rows = cross.paired_rows(errors, bootstraps, [("reference", "model")])
        macro = next(row for row in rows if row["cohort"] == "MACRO")

        cohort_draws = []
        expected_means = []
        for number, cohort in enumerate(("CMI", "MESA")):
            delta = (errors["reference"].xs(cohort).joint_mae_min
                     - errors["model"].xs(cohort).joint_mae_min).to_numpy()
            indices = np.random.default_rng(23 + number).integers(
                0, len(delta), size=(100, len(delta)),
            )
            cohort_draws.append(delta[indices].mean(axis=1))
            expected_means.append(delta.mean())
        expected_draws = (cohort_draws[0] + cohort_draws[1]) / 2

        self.assertAlmostEqual(macro["improvement_min"], np.mean(expected_means))
        np.testing.assert_allclose(
            [macro["ci95_low"], macro["ci95_high"]],
            np.quantile(expected_draws, [.025, .975]), atol=1e-12, rtol=0,
        )

    def test_frozen_loader_rejects_any_manifested_artifact_change_before_loading_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            artifact = source / "audit.csv"
            artifact.write_text("locked\n", encoding="utf-8")
            protocol = {
                "database_sha256": "database-hash",
                "code_sha256": cross.pt.sha256(cross.cc.__file__),
            }
            cross.pt.write_json(source / "protocol.json", protocol)
            selection = {
                "common_config": {"candidate": "huber", "preprocessing": "none"},
                "protocol_sha256": cross.pt.sha256(source / "protocol.json"),
                "models_sha256": {},
            }
            cross.pt.write_json(source / "selection_lock.json", selection)
            cross.pt.write_json(source / "manifest.json", {
                "database_sha256": "database-hash",
                "artifacts_sha256": {"audit.csv": cross.pt.sha256(artifact)},
            })
            artifact.write_text("tampered\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Changed source artifact"):
                cross.load_frozen_sources(
                    source, "database-hash",
                    pd.DataFrame({"cohort": [], "subject_id": [], "split": []}),
                )


if __name__ == "__main__":
    unittest.main()
