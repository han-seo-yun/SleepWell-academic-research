import unittest
import pandas as pd
from package_research_support import MMASH_FEATURES, prepare_mmash, small_cohort_split


class SupportTests(unittest.TestCase):
    def fixture(self):
        d = pd.DataFrame({"subject_id": ["1", "1", "2"], "epoch_start_sec": [0, 30, 0],
                          "epoch_end_sec": [30, 60, 30], "row_quality_ok": [True]*3,
                          "minutes_to_out_bed": [120, -1, 15], "rr_hr_mean_30s": [999]*3,
                          "seconds_to_out_bed": [7200, -60, 900], "wake_within_15m": [0, 0, 1]})
        for key in MMASH_FEATURES:
            d[key] = 1.
        # Ensure an actual three-way cohort; preserve the invalid row above.
        copies = [d]
        for i in range(3, 17):
            part = d.iloc[[0]].copy()
            part["subject_id"] = str(i)
            copies.append(part)
        return pd.concat(copies, ignore_index=True)

    def test_forbidden_features_and_post_event_exclusion(self):
        result = prepare_mmash(self.fixture())
        self.assertEqual(len(result), 16)
        self.assertNotIn("rr_hr_mean_30s", result)
        self.assertNotIn("wake_within_15m", result)
        self.assertNotIn("seconds_to_out_bed", result)
        self.assertTrue(result.subject_id.str.startswith("mmash_").all())

    def test_duplicate_rejected(self):
        d = self.fixture()
        with self.assertRaises(ValueError):
            prepare_mmash(pd.concat([d, d.iloc[:1]]))

    def test_small_cohort_has_actual_test_people(self):
        split = small_cohort_split([str(i) for i in range(16)])
        self.assertEqual(list(split.values()).count("train"), 11)
        self.assertEqual(list(split.values()).count("validation"), 2)
        self.assertEqual(list(split.values()).count("test"), 3)

    def test_nonfinite_feature_rejected(self):
        d = self.fixture()
        d.loc[0, MMASH_FEATURES[0]] = float("inf")
        with self.assertRaises(ValueError):
            prepare_mmash(d)


if __name__ == "__main__":
    unittest.main()
