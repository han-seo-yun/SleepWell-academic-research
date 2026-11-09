from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


class SampleDataTests(unittest.TestCase):
    def test_samples_have_expected_roles_and_mixed_types(self) -> None:
        binary = pd.read_csv(ROOT / "samples" / "classification_mixed.csv")
        regression = pd.read_excel(ROOT / "samples" / "regression_mixed.xlsx")
        dual = pd.read_csv(ROOT / "samples" / "dual_tasks.csv")
        multiclass = pd.read_csv(ROOT / "samples" / "multiclass_mixed.csv")
        self.assertEqual(set(binary["response"].unique()), {"yes", "no"})
        self.assertTrue(pd.api.types.is_numeric_dtype(regression["outcome_score"]))
        self.assertIn("clinical_response", dual)
        self.assertEqual(set(multiclass["risk_group"].unique()), {"low", "medium", "high"})
        self.assertTrue(binary["biomarker"].isna().any())
        self.assertGreater(binary["group_id"].duplicated().sum(), 0)


if __name__ == "__main__":
    unittest.main()
