import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from train_ridge_residual import circular_residual, train_and_export


class RidgeExportTest(unittest.TestCase):
    def test_circular_residual_handles_midnight(self):
        actual = pd.Series([10.0, 1430.0])
        physical = pd.Series([1430.0, 10.0])
        np.testing.assert_allclose(circular_residual(actual, physical), [20.0, -20.0])

    def test_export_reproduces_manual_kotlin_equation(self):
        rng = np.random.default_rng(7)
        n = 60
        x1 = rng.normal(size=n)
        x2 = rng.normal(size=n)
        x2[5] = np.nan
        physical_bed = np.full(n, 1380.0)
        physical_wake = np.full(n, 420.0)
        bed_residual = 12.0 + 8.0 * x1 + np.nan_to_num(x2, nan=0.0) * -3.0
        wake_residual = -4.0 + 2.0 * x1 + np.nan_to_num(x2, nan=0.0) * 5.0
        frame = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=n).astype(str),
            "x1": x1,
            "x2": x2,
            "actual_bedtime_minute": (physical_bed + bed_residual) % 1440,
            "actual_wake_minute": (physical_wake + wake_residual) % 1440,
            "physical_bedtime_minute": physical_bed,
            "physical_wake_minute": physical_wake,
        })
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "daily.csv"
            target = Path(td) / "ridge.json"
            frame.to_csv(source, index=False)
            payload = train_and_export(source, target, features=["x1", "x2"], alphas=(0.1, 1.0))
            loaded = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(payload, loaded)
        self.assertEqual(loaded["feature_order"], ["x1", "x2"])
        self.assertLess(loaded["training"]["test_metrics"]["bedtime_mae_min"], 2.0)
        self.assertLess(loaded["training"]["test_metrics"]["wake_mae_min"], 2.0)

        prep = loaded["preprocessing"]
        raw = np.array([x1[-1], x2[-1]])
        z = (raw - np.array(prep["mean"])) / np.array(prep["scale"])
        target_model = loaded["targets"]["bedtime_residual_min"]
        manual = target_model["intercept"] + np.dot(target_model["coefficients"], z)
        self.assertAlmostEqual(float(manual), float(bed_residual[-1]), delta=2.0)

    def test_rejects_target_leakage(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "empty.csv"
            p.write_text("date\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "미래/정답"):
                train_and_export(p, Path(td) / "out.json", features=["actual_wake_minute"])


if __name__ == "__main__":
    unittest.main()
