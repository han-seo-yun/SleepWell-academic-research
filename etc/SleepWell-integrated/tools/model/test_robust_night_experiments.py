import sys
import tempfile
import unittest
import subprocess
import json
from pathlib import Path

import numpy as np
import pandas as pd
import joblib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from robust_night_experiments import TrainQuantileClip, balanced_weights, replay, target_flagger, make_model


class RobustExperimentTests(unittest.TestCase):
    def test_predictor_clip_does_not_learn_from_validation(self):
        train = np.arange(100., dtype=float).reshape(-1, 1)
        clip = TrainQuantileClip().fit(train)
        before = (clip.low_.copy(), clip.high_.copy())
        transformed = clip.transform([[1e12], [-1e12]])
        np.testing.assert_array_equal(clip.low_, before[0])
        np.testing.assert_array_equal(clip.high_, before[1])
        self.assertAlmostEqual(transformed[0, 0], 98.01)

    def test_each_participant_has_equal_total_fit_weight(self):
        frame = pd.DataFrame({"subject_id": ["a"] * 10 + ["b"] * 2})
        weights = balanced_weights(frame)
        self.assertAlmostEqual(weights[:10].sum(), weights[10:].sum())

    def test_rarity_flag_keeps_extreme_target_unchanged(self):
        train = pd.DataFrame({"bed_offset": [100., 120., 140.], "wake_offset": [600., 610., 620.]})
        evaluation = pd.DataFrame({"bed_offset": [900.], "wake_offset": [1400.]})
        flags, _ = target_flagger(train, evaluation)
        self.assertTrue(flags[0])
        self.assertEqual(evaluation.bed_offset.iloc[0], 900.)

    def test_fold_reset_and_future_outcome_invariance(self):
        rows = []
        for i in range(6):
            cutoff = pd.Timestamp("2026-01-01T20:00:00+09:00") + pd.Timedelta(days=i)
            rows.append({"row_id": i, "cohort": "samsung", "subject_id": "one",
                         "split": "train" if i < 3 else "validation", "cutoff": cutoff,
                         "bed": cutoff+pd.Timedelta(hours=3), "wake": cutoff+pd.Timedelta(hours=11),
                         "observed_at": cutoff+pd.Timedelta(hours=11)})
        frame = pd.DataFrame(rows)
        prior = {"bed_offset": 180., "wake_offset": 660.}
        original, states = replay(frame, prior)
        self.assertEqual(states.valid_days.tolist(), [0, 1, 2, 0, 1, 2])
        changed = frame.copy()
        changed.loc[5, "bed"] += pd.Timedelta(hours=2)
        perturbed, _ = replay(changed, prior)
        pd.testing.assert_frame_equal(original, perturbed)

    def test_serialized_estimator_preserves_predictions(self):
        rng = np.random.default_rng(7)
        x = rng.normal(size=(50, 4))
        y = np.column_stack([100 + 10*x[:, 0], 600 + 20*x[:, 1]])
        x[:, 3] = 0  # Removed inside fitted pipeline.
        model = make_model("ridge100").fit(x, y)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "model.joblib"
            joblib.dump(model, path)
            np.testing.assert_allclose(joblib.load(path).predict(x), model.predict(x), atol=1e-10)
            command = [
                sys.executable, "-B", "-c",
                "import sys,json,joblib;sys.path.insert(0,sys.argv[1]);m=joblib.load(sys.argv[2]);print(json.dumps(m.predict([[0,0,0,0]]).tolist()))",
                str(Path(__file__).resolve().parent), str(path),
            ]
            child = subprocess.run(command, check=True, text=True, capture_output=True)
            np.testing.assert_allclose(json.loads(child.stdout), model.predict([[0, 0, 0, 0]]), atol=1e-10)
            self.assertEqual(type(model.named_steps["clip"]).__module__, "night_estimators")


if __name__ == "__main__":
    unittest.main()
