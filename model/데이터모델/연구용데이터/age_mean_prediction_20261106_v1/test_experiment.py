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


class AgeContextTests(unittest.TestCase):
    def setUp(self):
        self.prior = experiment.load_prior()
        self.frame = pd.DataFrame({"age": [54.0, 60.0, 80.0, 81.0, np.nan]})

    def test_control_is_constant_and_age_uses_only_frozen_age_means(self):
        control = experiment.age_context(self.frame, self.prior, "control") * 60.0
        age = experiment.age_context(self.frame, self.prior, "age") * 60.0
        np.testing.assert_allclose(control, self.prior["global_mean"])
        np.testing.assert_allclose(age, [self.prior["age_only"]["45-59"],
            self.prior["age_only"]["60-80"], self.prior["age_only"]["60-80"],
            self.prior["global_mean"], self.prior["global_mean"]])

    def test_band_is_order_equivalent_to_mean_including_global_fallback(self):
        selected = self.frame.iloc[[0, 1, 3]]
        age = experiment.age_context(selected, self.prior, "age")
        band = experiment.age_context(selected, self.prior, "band")
        np.testing.assert_array_equal(np.argsort(age), np.argsort(band))
        self.assertLess(band[0], band[2])
        self.assertLess(band[2], band[1])

    def test_all_arms_add_one_feature_and_reuse_train_spec(self):
        spec = experiment.fit_transform_spec(np.asarray([[1.0, np.nan], [3.0, 4.0]]))
        validation = np.asarray([[np.nan, np.nan]])
        frame = pd.DataFrame({"age": [60.0]})
        for arm in experiment.ARMS:
            transformed = experiment.matrix(validation, frame, spec, self.prior, arm)
            self.assertEqual(transformed.shape, (1, 3))
            np.testing.assert_allclose(transformed[0, :2], [2.0, 4.0])


class SplitAndWeightTests(unittest.TestCase):
    def test_weights_give_each_person_equal_total_weight(self):
        frame = pd.DataFrame({"subject_id": ["many"] * 4 + ["one"]})
        weights = experiment.participant_weights(frame)
        totals = pd.Series(weights).groupby(frame.subject_id).sum()
        self.assertAlmostEqual(totals["many"], totals["one"])
        self.assertAlmostEqual(weights.mean(), 1.0)

    def test_split_contract_rejects_participant_overlap(self):
        rows = []
        for split, (people, nights) in experiment.EXPECTED_SPLITS.items():
            for index in range(nights):
                rows.append({"subject_id": f"{split}-{index % people}", "split": split,
                             "cutoff": f"{split}-{index}"})
        frame = pd.DataFrame(rows)
        experiment.verify_split(frame)
        frame.loc[frame.subject_id.eq("validation-0"), "subject_id"] = "train-0"
        with self.assertRaisesRegex(ValueError, "crosses frozen splits"):
            experiment.verify_split(frame)

    def test_summary_and_bootstrap_balance_people_not_nights(self):
        frame = pd.DataFrame({"subject_id": ["many"] * 10 + ["one"],
                              "bedtime_min": np.zeros(11), "wake_min": np.zeros(11)})
        comparator = np.asarray([[10.0, 10.0]] * 10 + [[0.0, 0.0]])
        candidate = np.zeros((11, 2))
        metrics = experiment.summary(frame, comparator)
        difference = experiment.paired(frame, comparator, candidate)
        self.assertEqual(metrics["people"], 2)
        self.assertEqual(metrics["nights"], 11)
        self.assertAlmostEqual(metrics["joint"], 5.0)
        self.assertAlmostEqual(difference["improvement_min"], 5.0)


class EligibilityAndFallbackTests(unittest.TestCase):
    def setUp(self):
        self.frame = feature_frame([
            {"subject_id": "a", "age": 54, "baseline_mesa_bed": 600,
             "baseline_mesa_wake": 800, "baseline_cmi_bed": 620, "baseline_cmi_wake": 820},
            {"subject_id": "b", "age": 60, "baseline_mesa_bed": 601,
             "baseline_mesa_wake": 801, "baseline_cmi_bed": 621, "baseline_cmi_wake": 821},
            {"subject_id": "c", "age": 81, "baseline_mesa_bed": 602,
             "baseline_mesa_wake": 802, "baseline_cmi_bed": 622, "baseline_cmi_wake": 822},
            {"subject_id": "d", "age": np.nan, "baseline_mesa_bed": 603,
             "baseline_mesa_wake": 803, "baseline_cmi_bed": 623, "baseline_cmi_wake": 823},
        ])
        self.spec = {"median": np.zeros(len(experiment.FEATURES)),
                     "low": np.full(len(experiment.FEATURES), -100.0),
                     "high": np.full(len(experiment.FEATURES), 100.0)}
        self.models = {arm: ConstantModel() for arm in experiment.ARMS}

    def test_scientific_and_deployable_ranges_are_distinct(self):
        np.testing.assert_array_equal(experiment.eligibility(self.frame, "scientific_54_80"),
                                      [True, True, False, False])
        np.testing.assert_array_equal(experiment.eligibility(self.frame, "deployable_60_80"),
                                      [False, True, False, False])

    def test_every_arm_restores_current_output_exactly_outside_scope(self):
        result = experiment.predictions(self.frame, self.models, self.spec,
                                        experiment.load_prior(), "deployable_60_80")
        outside = np.asarray([True, False, True, True])
        for arm in experiment.ARMS:
            np.testing.assert_array_equal(result[arm][outside], result["current_v8"][outside])
            self.assertFalse(np.array_equal(result[arm][~outside], result["current_v8"][~outside]))

    def test_projection_is_exact_current_reference_constraint(self):
        raw = np.asarray([[-10.0, 2000.0], [100.0, 110.0]])
        expected = np.asarray([experiment.constrain(*row) for row in raw])
        np.testing.assert_array_equal(experiment.project(raw), expected)


class GateTests(unittest.TestCase):
    @staticmethod
    def metrics(joint, people=40):
        return {"people": people, "nights": people, "joint": joint,
                "onset": joint, "wake": joint, "interval": joint}

    def test_gate_ignores_diagnostic_band_and_fails_on_age_candidate(self):
        scopes = {"deployable_60_80": {}}
        for split in ("validation", "test"):
            views = {}
            for view in ("all_with_exact_fallback", "eligible_only"):
                views[view] = {"metrics": {
                    "current_v8": self.metrics(10), "control": self.metrics(9),
                    "age": self.metrics(9.5), "band": self.metrics(0)},
                    "age_vs": {"current_v8": {"ci95": [-1, 2]},
                               "control": {"ci95": [-1, 2]}}}
            strata = {"eligible_history_0": {"metrics": {
                "current_v8": self.metrics(10), "control": self.metrics(9),
                "age": self.metrics(9.5), "band": self.metrics(0)}}}
            scopes["deployable_60_80"][split] = {"views": views, "strata": strata}

        decision = experiment.gate({"scopes": scopes})
        self.assertFalse(decision["performance_pass"])
        self.assertEqual(decision["decision"], "do_not_integrate")
        self.assertTrue(all("band" not in name for name in decision["checks"]))


if __name__ == "__main__":
    unittest.main()
