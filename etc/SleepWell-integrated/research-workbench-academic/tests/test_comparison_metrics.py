from __future__ import annotations

import copy
import math
import unittest

from researchbench.comparison_metrics import comparison_data


def prediction(participant, cutoff, model, actual, predicted, **extra):
    return {
        "participant_id": participant, "cutoff": cutoff, "model_id": model,
        "actual_onset_offset_min": actual, "onset_offset_min": predicted,
        "actual_wake_offset_min": actual + 400 if actual is not None else None,
        "wake_offset_min": predicted + 400 if predicted is not None else None,
        "actual_interval_min": 400, "interval_min": 400 + (predicted - actual)
            if predicted is not None and actual is not None else None,
        **extra,
    }


class ComparisonMetricsTests(unittest.TestCase):
    def setUp(self):
        rows = []
        for model, errors in {"a": [0, 2, 10], "b": [1, 1, 4]}.items():
            for (participant, cutoff, actual), error in zip(
                    [("p1", "d1", 100), ("p1", "d2", 120), ("p2", "d1", 140)], errors):
                rows.append(prediction(participant, cutoff, model, actual, actual + error))
        self.result = {"raw": {"predictions": rows}, "summary": [{"untouched": True}]}

    def test_participant_equal_matches_preserved_estimand_and_does_not_mutate(self):
        original = copy.deepcopy(self.result)
        data = comparison_data(self.result, target="onset", tolerance=2)
        a = next(row for row in data["rows"] if row["model_id"] == "a")
        self.assertAlmostEqual(a["mae"], 5.5)
        self.assertAlmostEqual(a["rmse"], (math.sqrt(2) + 10) / 2)
        self.assertAlmostEqual(a["bias"], 5.5)
        self.assertAlmostEqual(a["within_tolerance"], .5)
        self.assertEqual((a["n_nights"], a["n_participants"]), (3, 2))
        self.assertEqual(self.result, original)
        self.assertTrue(any("ROC-AUC" in warning for warning in data["warnings"]))

    def test_night_equal_and_tolerance_are_explicit(self):
        data = comparison_data(self.result, model_ids=["a"], averaging="night_equal", tolerance=2)
        row = data["rows"][0]
        self.assertAlmostEqual(row["mae"], 4)
        self.assertAlmostEqual(row["rmse"], math.sqrt(104 / 3))
        self.assertAlmostEqual(row["within_tolerance"], 2 / 3)

    def test_common_valid_nights_and_exclusion_counts(self):
        altered = copy.deepcopy(self.result)
        altered["raw"]["predictions"][3]["onset_offset_min"] = float("nan")
        altered["raw"]["predictions"].append(prediction("p3", "d1", "a", 80, 81))
        data = comparison_data(altered, model_ids=["a", "b"])
        self.assertTrue(data["available"])
        self.assertEqual(data["n_nights"], 2)
        self.assertEqual(data["excluded_counts"]["a"]["outside_common_nights"], 2)
        self.assertEqual(data["excluded_counts"]["b"]["invalid_pairs"], 1)
        self.assertEqual({(row["participant_id"], row["cutoff"]) for row in data["predictions"]},
                         {("p1", "d2"), ("p2", "d1")})

    def test_duplicate_keys_and_truth_mismatch_fail_closed(self):
        duplicate = copy.deepcopy(self.result)
        duplicate["raw"]["predictions"].append(copy.deepcopy(duplicate["raw"]["predictions"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate_prediction_key"):
            comparison_data(duplicate)
        mismatch = copy.deepcopy(self.result)
        mismatch["raw"]["predictions"][3]["actual_onset_offset_min"] = 101
        with self.assertRaisesRegex(ValueError, "actual_value_mismatch"):
            comparison_data(mismatch)

    def test_duplicate_key_is_rejected_even_when_one_pair_is_missing(self):
        duplicate = copy.deepcopy(self.result)
        repeated = copy.deepcopy(duplicate["raw"]["predictions"][0])
        repeated["onset_offset_min"] = None
        duplicate["raw"]["predictions"].append(repeated)
        with self.assertRaisesRegex(ValueError, "duplicate_prediction_key"):
            comparison_data(duplicate)

    def test_blank_participant_or_cutoff_is_invalid(self):
        altered = copy.deepcopy(self.result)
        altered["raw"]["predictions"].append(prediction("  ", "d4", "a", 100, 101))
        altered["raw"]["predictions"].append(prediction("p4", "", "b", 100, 101))
        data = comparison_data(altered)
        self.assertEqual(data["excluded_counts"]["a"]["invalid_pairs"], 1)
        self.assertEqual(data["excluded_counts"]["b"]["invalid_pairs"], 1)

    def test_empty_summary_is_not_used_as_prediction_evidence(self):
        data = comparison_data({"summary": [{"model_id": "a", "onset_mae_min": 5}]})
        self.assertFalse(data["available"])
        self.assertIn("summary values were not substituted", data["warnings"][0])

    def test_no_common_nights_and_invalid_arguments(self):
        rows = [prediction("p1", "d1", "a", 100, 101), prediction("p2", "d1", "b", 100, 102)]
        data = comparison_data({"raw": {"predictions": rows}})
        self.assertFalse(data["available"])
        self.assertTrue(any("no common" in warning for warning in data["warnings"]))
        for kwargs in ({"target": "bad"}, {"averaging": "bad"}, {"tolerance": -1}):
            with self.assertRaises(ValueError):
                comparison_data(self.result, **kwargs)


if __name__ == "__main__":
    unittest.main()
