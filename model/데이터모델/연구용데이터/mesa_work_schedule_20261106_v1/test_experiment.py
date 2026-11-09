import unittest

import numpy as np
import pandas as pd

import experiment


def feature_frame(rows):
    frame = pd.DataFrame(rows)
    for name in experiment.FEATURES:
        frame[f"f__{name}"] = 0.0
    return frame


class ConstantModel:
    def __init__(self, bed=700.0, wake=900.0):
        self.value = np.asarray([bed, wake], dtype=float)

    def predict(self, values):
        return np.tile(self.value, (len(values), 1))


class MetricContractTests(unittest.TestCase):
    def test_summary_and_paired_comparison_balance_people_not_nights(self):
        frame = pd.DataFrame(
            {
                "subject_id": ["many"] * 10 + ["one"],
                "bedtime_min": [0.0] * 11,
                "wake_min": [0.0] * 11,
            }
        )
        comparator = np.asarray([[10.0, 10.0]] * 10 + [[0.0, 0.0]])
        candidate = np.zeros((11, 2), dtype=float)

        summary = experiment.summary(frame, comparator)
        comparison = experiment.paired(frame, comparator, candidate)

        self.assertEqual(summary["people"], 2)
        self.assertEqual(summary["nights"], 11)
        self.assertAlmostEqual(summary["joint"], 5.0)
        self.assertAlmostEqual(comparison["improvement_min"], 5.0)
        self.assertNotAlmostEqual(summary["joint"], 100.0 / 11.0)


class TransformationContractTests(unittest.TestCase):
    def test_schedule_encoding_is_nominal_with_explicit_unknown(self):
        x = np.asarray([[1.0], [2.0], [3.0]])
        frame = pd.DataFrame(
            {
                "work_schedule_code": pd.Series([1, 7, pd.NA], dtype="Int64"),
                "extra_hours_days_per_month": pd.Series([0, 0, pd.NA], dtype="Int64"),
            }
        )
        spec = {
            "median": np.asarray([2.0]),
            "low": np.asarray([1.0]),
            "high": np.asarray([3.0]),
            "extra_median": 10.0,
        }

        encoded = experiment.matrix(x, frame, spec, "schedule")[:, 1:]

        self.assertEqual(encoded.shape, (3, 8))
        np.testing.assert_array_equal(encoded[0], [1, 0, 0, 0, 0, 0, 0, 0])
        np.testing.assert_array_equal(encoded[1], [0, 0, 0, 0, 0, 0, 1, 0])
        np.testing.assert_array_equal(encoded[2], [0, 0, 0, 0, 0, 0, 0, 1])

    def test_train_medians_are_reused_for_missing_values_without_zero_fill(self):
        train_x = np.asarray([[1.0, np.nan], [3.0, 4.0]])
        train = pd.DataFrame(
            {
                "extra_hours_days_per_month": pd.Series([10, pd.NA], dtype="Int64"),
                "work_schedule_code": pd.Series([1, 1], dtype="Int64"),
            }
        )
        spec = experiment.fit_transform_spec(train_x, train)
        validation = pd.DataFrame(
            {
                "extra_hours_days_per_month": pd.Series([pd.NA], dtype="Int64"),
                "work_schedule_code": pd.Series([1], dtype="Int64"),
            }
        )

        control = experiment.matrix(np.asarray([[np.nan, np.nan]]), validation, spec, "control")
        enriched = experiment.matrix(
            np.asarray([[np.nan, np.nan]]), validation, spec, "schedule_extra"
        )

        np.testing.assert_allclose(control[0], [2.0, 4.0])
        self.assertAlmostEqual(enriched[0, -2], 10.0 / 31.0)
        self.assertEqual(enriched[0, -1], 1.0)
        self.assertFalse(np.any(control == 0.0))


class EligibilityAndFallbackTests(unittest.TestCase):
    def test_unsupported_missing_and_out_of_scope_ages_are_ineligible(self):
        frame = pd.DataFrame(
            {
                "work_schedule_code": pd.Series([1, 2, pd.NA, 1, 1], dtype="Int64"),
                "age": [60, 70, 70, 59, 81],
            }
        )
        np.testing.assert_array_equal(
            experiment.eligible(frame, supported=[1]),
            [True, False, False, False, False],
        )

    def test_full_prediction_restores_current_v8_for_every_ineligible_row(self):
        frame = feature_frame(
            [
                {"work_schedule_code": 1, "age": 60, "baseline_mesa_bed": 600,
                 "baseline_mesa_wake": 800, "baseline_cmi_bed": 620, "baseline_cmi_wake": 820},
                {"work_schedule_code": 2, "age": 70, "baseline_mesa_bed": 601,
                 "baseline_mesa_wake": 801, "baseline_cmi_bed": 621, "baseline_cmi_wake": 821},
                {"work_schedule_code": pd.NA, "age": 70, "baseline_mesa_bed": 602,
                 "baseline_mesa_wake": 802, "baseline_cmi_bed": 622, "baseline_cmi_wake": 822},
                {"work_schedule_code": 1, "age": 81, "baseline_mesa_bed": 603,
                 "baseline_mesa_wake": 803, "baseline_cmi_bed": 623, "baseline_cmi_wake": 823},
            ]
        )
        frame["work_schedule_code"] = frame["work_schedule_code"].astype("Int64")
        spec = {
            "median": np.zeros(len(experiment.FEATURES)),
            "low": np.full(len(experiment.FEATURES), -100.0),
            "high": np.full(len(experiment.FEATURES), 100.0),
            "extra_median": 0.0,
        }

        result = experiment.predictions(
            frame, {"schedule": ConstantModel()}, spec, supported=[1], guarded=True
        )

        np.testing.assert_array_equal(result["schedule"][1:], result["current_v8"][1:])
        self.assertFalse(np.array_equal(result["schedule"][0], result["current_v8"][0]))


class CausalProviderTests(unittest.TestCase):
    def test_post_cutoff_target_record_cannot_change_features_or_prediction(self):
        model = experiment.PortableCohortModel(
            experiment.ASSETS / "robust_mesa_v2.json", "mesa"
        )
        cutoff = experiment.local_time("2026-10-20T20:00:00+09:00")
        profile = {"created_at": "2026-10-01T10:00:00+09:00"}
        eligible = {
            "source": "research",
            "bed": "2026-10-18T23:00:00+09:00",
            "wake": "2026-10-19T07:00:00+09:00",
            "observed_at": "2026-10-19T08:00:00+09:00",
        }
        future_target = {
            "source": "research",
            "bed": "2026-10-20T23:00:00+09:00",
            "wake": "2026-10-21T07:00:00+09:00",
            "observed_at": "2026-10-21T08:00:00+09:00",
        }

        known = model.predict(
            profile, [eligible], cutoff, personalization_policy=experiment.GRADUAL_POLICY
        )
        contaminated = model.predict(
            profile,
            [eligible, future_target],
            cutoff,
            personalization_policy=experiment.GRADUAL_POLICY,
        )

        self.assertEqual(known["features"], contaminated["features"])
        self.assertEqual(known["hybrid"], contaminated["hybrid"])
        self.assertEqual(known["personalization"], contaminated["personalization"])


if __name__ == "__main__":
    unittest.main()
