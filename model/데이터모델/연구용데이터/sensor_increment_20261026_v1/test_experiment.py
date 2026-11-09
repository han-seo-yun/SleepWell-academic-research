from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_experiment as experiment


class HistoryFeatureTests(unittest.TestCase):
    @staticmethod
    def _night(
        cutoff: str,
        onset: float,
        wake: float,
        observed_at: str | None = None,
        split: str = "train",
    ) -> dict[str, object]:
        cutoff_at = pd.Timestamp(cutoff)
        sleep_start = cutoff_at + pd.Timedelta(minutes=onset)
        sleep_end = cutoff_at + pd.Timedelta(minutes=wake)
        return {
            "participant_id": "P01",
            "target_id": cutoff_at.isoformat(),
            "split": split,
            "cutoff": cutoff_at,
            "sleep_start": sleep_start,
            "sleep_end": sleep_end,
            "observed_at": pd.Timestamp(observed_at) if observed_at else sleep_end,
            experiment.TARGETS[0]: onset,
            experiment.TARGETS[1]: wake,
            experiment.TARGETS[2]: wake - onset,
        }

    def test_future_and_late_records_are_excluded_and_future_mutation_is_invariant(self) -> None:
        rows = [
            self._night("2024-01-01T20:00:00+09:00", 100.0, 500.0),
            self._night(
                "2024-01-02T20:00:00+09:00",
                200.0,
                600.0,
                observed_at="2024-01-04T20:00:00+09:00",
            ),
            self._night("2024-01-04T20:00:00+09:00", 300.0, 700.0),
            self._night("2024-01-05T20:00:00+09:00", 400.0, 800.0),
        ]
        targets = pd.DataFrame(rows)
        original = experiment.derive_history_features(targets)
        target_history = original.loc[2]
        self.assertEqual(target_history["history_observation_count"], 1.0)
        self.assertEqual(target_history["previous_onset_offset_min"], 100.0)
        self.assertLess(target_history["max_history_wake_at"], targets.loc[2, "cutoff"])
        self.assertLess(target_history["max_history_observed_at"], targets.loc[2, "cutoff"])

        mutated = targets.copy()
        mutated.loc[2, list(experiment.TARGETS)] = [1.0, 31.0, 30.0]
        mutated.loc[3, list(experiment.TARGETS)] = [9000.0, 9999.0, 999.0]
        mutated.loc[3, "sleep_end"] = pd.Timestamp("2024-01-02T02:00:00+09:00")
        mutated.loc[3, "observed_at"] = pd.Timestamp("2024-01-02T03:00:00+09:00")
        after = experiment.derive_history_features(mutated)
        compare_columns = [
            *[name for name in experiment.HISTORY_FEATURES if not name.startswith("weekday_") and name != "days_from_training_start"],
            "max_history_source_cutoff",
            "max_history_wake_at",
            "max_history_observed_at",
        ]
        pd.testing.assert_series_equal(
            original.loc[2, compare_columns],
            after.loc[2, compare_columns],
            check_names=False,
        )

    def test_wake_and_observed_at_must_both_be_strictly_before_cutoff(self) -> None:
        rows = [
            self._night(
                "2024-01-01T20:00:00+09:00",
                100.0,
                500.0,
                observed_at="2024-01-03T20:00:00+09:00",
            ),
            self._night("2024-01-03T20:00:00+09:00", 200.0, 600.0),
        ]
        history = experiment.derive_history_features(pd.DataFrame(rows))
        self.assertEqual(history.loc[1, "history_observation_count"], 0.0)
        self.assertTrue(np.isnan(history.loc[1, "previous_onset_offset_min"]))


class SensorJoinAndSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cutoffs = pd.to_datetime(
            ["2024-01-01T20:00:00+09:00", "2024-01-02T20:00:00+09:00"],
            utc=True,
        ).tz_convert(experiment.KST)
        self.panel = pd.DataFrame(
            {"participant_id": ["P01", "P01"], "cutoff": self.cutoffs, "value": [1, 2]}
        )
        self.cache = pd.DataFrame({"cutoff": self.cutoffs, "sensor": [3, 4]})

    def test_cutoff_join_is_one_to_one_and_complete(self) -> None:
        joined = experiment.join_sensor_cache(self.panel, self.cache)
        self.assertEqual(joined["sensor"].tolist(), [3, 4])
        duplicate = pd.concat([self.cache, self.cache.iloc[[0]]], ignore_index=True)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            experiment.join_sensor_cache(self.panel, duplicate)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            experiment.join_sensor_cache(self.panel, self.cache.iloc[[0]])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            experiment.join_sensor_cache(pd.concat([self.panel, self.panel.iloc[[0]]]), self.cache)

    def test_missing_export_bins_are_not_a_true_zero_measurement(self) -> None:
        cache = pd.DataFrame(
            {
                "hr_mean": [0.0, 0.0],
                "hr_std": [0.0, 0.0],
                "hr_count": [0, 2],
                "late_hr_mean": [0.0, 0.0],
                "late_hr_count": [0, 1],
                "steps": [0.0, 0.0],
                "step_bins": [0, 1],
            }
        )
        derived = experiment.derive_sensor_features(cache)
        self.assertTrue(np.isnan(derived.loc[0, "hr_mean"]))
        self.assertEqual(derived.loc[1, "hr_mean"], 0.0)
        self.assertTrue(np.isnan(derived.loc[0, "log1p_steps"]))
        self.assertEqual(derived.loc[1, "log1p_steps"], 0.0)
        self.assertFalse(bool(derived.loc[0, "step_export_bins_present"]))
        self.assertTrue(bool(derived.loc[1, "step_export_bins_present"]))
        self.assertEqual(derived.loc[0, "log1p_step_bins"], 0.0)


class AssociationTests(unittest.TestCase):
    def test_bh_uses_fixed_family_of_twelve_including_p_one(self) -> None:
        adjusted = experiment.benjamini_hochberg_fixed([0.01, *([1.0] * 11)])
        self.assertAlmostEqual(adjusted[0], 0.12)
        self.assertEqual(adjusted[1:], [1.0] * 11)
        with self.assertRaises(ValueError):
            experiment.benjamini_hochberg_fixed([0.01] * 11)

    def test_zero_variance_is_unestimable_with_p_one(self) -> None:
        frame = pd.DataFrame(
            {
                "cutoff": pd.date_range("2024-01-01", periods=6, freq="D", tz=experiment.KST),
                "sensor": np.ones(6),
                "target": np.arange(6, dtype=float),
                "control": np.arange(6, dtype=float),
            }
        )
        result = experiment.association_stat(
            frame, "sensor", "target", ["control"], minimum_rows=4, compute_null=True
        )
        self.assertEqual(result["status"], "zero_variance_sensor_or_target")
        self.assertEqual(result["cyclic_shift_p_value"], 1.0)
        self.assertEqual(result["nonzero_rotation_count"], 0)

    def test_cyclic_null_uses_every_nonzero_rotation_and_finite_sample_p(self) -> None:
        left = np.array([-2.0, -1.0, 1.0, 2.0])
        right = np.array([-2.0, -1.0, 1.0, 2.0])
        p_value, rotations = experiment.cyclic_shift_p(left, right)
        self.assertEqual(rotations, 3)
        self.assertIn(p_value, {0.25, 0.5, 0.75, 1.0})


class SelectionAndMetricTests(unittest.TestCase):
    @staticmethod
    def _selection_predictions() -> pd.DataFrame:
        validation_errors = {
            "recent7_median": 1.0,
            experiment.REFERENCE_ARM: 20.0,
            "sensor_hr_mean_std": 14.0,
            "sensor_evening_hr_delta": 17.0,
            "sensor_log_steps": 18.0,
            "sensor_all_four": 16.0,
        }
        rows: list[dict[str, object]] = []
        for split in ("validation", "test"):
            for night in range(2):
                cutoff = pd.Timestamp(f"2024-01-{night + (1 if split == 'validation' else 11):02d}T20:00:00+09:00")
                for arm, validation_error in validation_errors.items():
                    error = validation_error if split == "validation" else 1000.0 - validation_error
                    rows.append(
                        {
                            "participant_id": "P01",
                            "target_id": f"{split}_{night}",
                            "split": split,
                            "cutoff": cutoff,
                            "arm": arm,
                            "joint_abs_error_min": error,
                            "actual_onset_offset_min": 100.0,
                            "actual_wake_offset_min": 500.0,
                        }
                    )
        return pd.DataFrame(rows)

    def test_selection_uses_validation_only_and_not_favorable_recent7(self) -> None:
        predictions = self._selection_predictions()
        selected = experiment.select_sensor_arm(predictions)
        self.assertEqual(selected["selected_arm"], "sensor_hr_mean_std")
        self.assertAlmostEqual(selected["best_sensor_gain_vs_history_coverage_min"], 6.0)
        self.assertFalse(selected["recent7_used_for_selection"])

        changed_test = predictions.copy()
        test_rows = changed_test["split"] == "test"
        changed_test.loc[test_rows, "joint_abs_error_min"] = np.arange(test_rows.sum()) * 10000.0
        changed_test.loc[test_rows, "actual_onset_offset_min"] = -999999.0
        changed_test.loc[test_rows, "actual_wake_offset_min"] = 999999.0
        self.assertEqual(
            experiment.select_sensor_arm(changed_test)["selected_arm"],
            selected["selected_arm"],
        )

    def test_exact_five_minute_validation_gain_passes_fixed_gate(self) -> None:
        predictions = self._selection_predictions()
        predictions.loc[
            (predictions["split"] == "validation")
            & (predictions["arm"] == "sensor_hr_mean_std"),
            "joint_abs_error_min",
        ] = 15.0
        selected = experiment.select_sensor_arm(predictions)
        self.assertEqual(selected["selected_arm"], "sensor_hr_mean_std")

    def test_paired_gain_requires_identical_same_night_keys(self) -> None:
        cutoffs = pd.to_datetime(
            ["2024-01-01T20:00:00+09:00", "2024-01-02T20:00:00+09:00"], utc=True
        ).tz_convert(experiment.KST)
        rows: list[dict[str, object]] = []
        for arm, errors in (("candidate", [5.0, 10.0]), ("reference", [10.0, 20.0])):
            for index, (cutoff, error) in enumerate(zip(cutoffs, errors)):
                rows.append(
                    {
                        "participant_id": "P01",
                        "target_id": f"night_{index}",
                        "split": "test",
                        "cutoff": cutoff,
                        "arm": arm,
                        "joint_abs_error_min": error,
                    }
                )
        predictions = pd.DataFrame(rows).sample(frac=1.0, random_state=7)
        paired = experiment.paired_night_gains(predictions, "candidate", "reference")
        self.assertEqual(sorted(paired["gain_min"].tolist()), [5.0, 10.0])
        self.assertAlmostEqual(paired["gain_min"].mean(), 7.5)

        missing = predictions.drop(predictions.index[(predictions["arm"] == "candidate")][0])
        with self.assertRaisesRegex(ValueError, "identical"):
            experiment.paired_night_gains(missing, "candidate", "reference")
        duplicate = pd.concat([predictions, predictions.iloc[[0]]], ignore_index=True)
        with self.assertRaisesRegex(ValueError, "one row"):
            experiment.paired_night_gains(duplicate, "candidate", "reference")


class FixedProtocolAndOutputGuardTests(unittest.TestCase):
    def test_pipeline_has_fixed_imputation_scaling_and_ridge(self) -> None:
        pipeline = experiment.make_ridge_pipeline()
        imputer = pipeline.named_steps["imputer"]
        self.assertEqual(imputer.strategy, "median")
        self.assertTrue(imputer.keep_empty_features)
        self.assertTrue(imputer.add_indicator)
        self.assertIsInstance(pipeline.named_steps["scaler"], experiment.StandardScaler)
        self.assertEqual(pipeline.named_steps["ridge"].alpha, 10.0)
        self.assertEqual(tuple(experiment.SENSOR_FEATURES), (
            "hr_mean",
            "hr_std",
            "evening_hr_delta",
            "log1p_steps",
        ))

    def test_output_guard_refuses_any_existing_planned_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            experiment.guard_outputs(output_dir)
            (output_dir / "panel.csv").write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                experiment.guard_outputs(output_dir)
            self.assertFalse((output_dir / "models").exists())

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            (output_dir / "models").mkdir()
            with self.assertRaises(FileExistsError):
                experiment.guard_outputs(output_dir)


if __name__ == "__main__":
    unittest.main()
