import sys
import unittest
import tempfile
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from next_night_experiments import score, prior_from_train, paired_interval, portable, canonical_cohorts, feature_tables
from sklearn.pipeline import make_pipeline
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler


class NightExperimentTests(unittest.TestCase):
    def test_samsung_replay_resets_each_split(self):
        rows = []
        for i in range(6):
            day = pd.Timestamp("2026-01-01T20:00:00+09:00") + pd.Timedelta(days=i)
            rows.append({"row_id": i, "cohort": "samsung", "subject_id": "one",
                         "split": ["train", "validation", "test"][i // 2],
                         "cutoff": day.isoformat(), "bed": (day + pd.Timedelta(hours=3)).isoformat(),
                         "wake": (day + pd.Timedelta(hours=11)).isoformat(),
                         "observed_at": (day + pd.Timedelta(hours=11)).isoformat()})
        f = pd.DataFrame(rows)
        _, _, states = feature_tables(f, {"bed_offset": 180, "wake_offset": 660})
        self.assertEqual(states.valid_days.tolist(), [0, 1, 0, 1, 0, 1])
        f["cohort"] = "cmi"
        _, _, group_states = feature_tables(f, {"bed_offset": 180, "wake_offset": 660})
        self.assertEqual(group_states.valid_days.tolist(), [0, 1, 2, 3, 4, 5])

    def test_builder_cohort_names_are_canonical_before_temporal_branch(self):
        source = pd.DataFrame({"cohort": ["CMI", "MESA", "Samsung"]})
        self.assertEqual(canonical_cohorts(source).cohort.tolist(), ["cmi", "mesa", "samsung"])
        self.assertEqual(source.cohort.iloc[2], "Samsung")
        with self.assertRaises(ValueError):
            canonical_cohorts(pd.DataFrame({"cohort": ["unknown"]}))

    def test_prior_excludes_validation_test_and_balances_people(self):
        f = pd.DataFrame({"subject_id": ["a", "a", "b", "z"], "split": ["train"]*3+["test"],
                          "bed_offset": [100, 100, 200, 900], "wake_offset": [600, 600, 700, 1400]})
        p = prior_from_train(f)
        self.assertEqual(p["bed_offset"], 150)
        self.assertEqual(p["wake_offset"], 650)

    def test_equal_participant_score_and_paired_sign(self):
        y = np.array([[100., 600.]]*3)
        baseline = y + np.array([[10., 10.], [10., 10.], [40., 40.]])
        subjects = ["a", "a", "b"]
        self.assertEqual(score(y, baseline, subjects)["joint_mae_min"], 25)
        result = paired_interval(y, baseline, y, subjects)
        self.assertEqual(result["metrics"]["joint"]["error_reduction_min"], 25)
        self.assertGreater(result["metrics"]["joint"]["ci95"][0], 0)

    def test_export_exact_predictions(self):
        x = np.array([[1.,2.], [3.,4.], [4.,8.], [5.,12.]])
        y = np.array([[100.,600.],[150.,630.],[200.,720.],[300.,780.]])
        model = make_pipeline(StandardScaler(), Ridge(alpha=10)).fit(x,y)
        raw = portable(model, ["x", "y"])
        z = (x - raw["mean"]) / raw["scale"]
        out = z @ np.array(raw["coefficients"]).T + raw["intercepts"]
        np.testing.assert_allclose(out, model.predict(x), atol=1e-10)


if __name__ == "__main__":
    unittest.main()
