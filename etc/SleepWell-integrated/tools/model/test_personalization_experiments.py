import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from personalization_experiments import invariance_checks, metrics, project, public_artifact, replay


class PersonalizationExperimentTests(unittest.TestCase):
    def test_runtime_invariance_checks(self):
        self.assertEqual(invariance_checks(), {
            "history_order_invariance": True,
            "current_future_outcome_invariance": True,
        })

    def test_replay_uses_only_earlier_resolved_outcomes(self):
        base_time = pd.Timestamp("2026-01-01T20:00:00+09:00")
        rows = []
        for index in range(15):
            cutoff = base_time + pd.Timedelta(days=index)
            rows.append({"cohort": "cmi", "subject_id": "a", "split": "test", "row_id": index,
                         "cutoff": cutoff, "observed_at": cutoff + pd.Timedelta(hours=12),
                         "wake": cutoff + pd.Timedelta(hours=12),
                         "bed_offset": 200.0, "wake_offset": 700.0})
        frame = pd.DataFrame(rows)
        base = np.tile([180.0, 680.0], (len(frame), 1))
        corrected, diagnostics = replay(frame, base, 14)
        self.assertTrue(np.allclose(corrected[:14], base[:14]))
        self.assertTrue(diagnostics.active.iloc[14])
        changed = frame.copy()
        changed.loc[14, ["bed_offset", "wake_offset"]] = [900.0, 1400.0]
        repeated, _ = replay(changed, base, 14)
        np.testing.assert_allclose(corrected, repeated)

    def test_projection_preserves_sleep_contract(self):
        result = project([[1000, -50]])[0]
        self.assertEqual(result[0], 960)
        self.assertEqual(result[1], 990)

    def test_metrics_support_empty_stratum(self):
        self.assertIsNone(metrics(np.empty((0, 2)), np.empty((0, 2)), [], True))

    def test_public_artifact_excludes_single_person_auxiliary_cohort(self):
        private = {"cohorts": {"cmi": {}, "mesa": {}, "samsung": {"private": True}},
                   "limitations": ["Samsung is one person and is auxiliary only.", "other"],
                   "provenance": {}}
        public = public_artifact(private)
        self.assertEqual(set(public["cohorts"]), {"cmi", "mesa"})
        self.assertFalse(public["privacy"]["personal_data_included"])
        self.assertIn("excluded", public["privacy"]["note"])


if __name__ == "__main__":
    unittest.main()
