import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from walch_experiments import (
    AGG_ACTIVITY_FEATURES,
    AGG_HR_FEATURES,
    aggregate_epoch_dataset,
    build_native_dataset,
    choose_winner,
    common_native_support,
    make_model,
    metrics,
    paired_subject_bootstrap,
    restrict_projection,
    run_experiments,
    subject_split,
)


class WalchExperimentsTests(unittest.TestCase):
    def test_subject_split_is_deterministic_and_disjoint(self):
        subjects = [f"s{i:02d}" for i in range(31)]
        first = subject_split(subjects)
        second = subject_split(reversed(subjects))
        self.assertEqual(first, second)
        self.assertEqual({key: len(value) for key, value in first.items()}, {
            "train": 22, "validation": 5, "test": 4,
        })
        self.assertEqual(set(first["train"]) & set(first["validation"]), set())
        self.assertEqual(set(first["train"]) & set(first["test"]), set())
        self.assertEqual(set(first["validation"]) & set(first["test"]), set())

    def test_raw_labels_override_cached_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            cache = root / "cache"
            (raw / "labels").mkdir(parents=True)
            cache.mkdir()
            pd.DataFrame({"epoch": [0, 30], "label": [0, 2]}).to_csv(
                raw / "labels" / "abc_labeled_sleep.csv", index=False
            )
            pd.DataFrame({
                "subject_id": ["abc", "abc"],
                "epoch_idx": [0, 1],
                "t_rel_sec": [0, 30],
                "activity_mean": [1.0, 2.0], "activity_std": [0.1, 0.2], "activity_n": [10, 10],
                "hr_mean": [60.0, 61.0], "hr_std": [1.0, 1.1], "hr_n": [2, 2],
                "sleep_stage_raw": [5, 0],  # deliberately contradictory and ignored
            }).to_csv(cache / "abc_unified_30s.csv", index=False)
            native, audit = build_native_dataset(raw, cache)
            self.assertEqual(native["is_sleep"].tolist(), [0, 1])
            self.assertFalse(audit["cached_label_columns_used"])
            self.assertEqual(audit["interpolation"], "none")

    def test_duplicate_raw_epoch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            cache = root / "cache"
            (raw / "labels").mkdir(parents=True)
            cache.mkdir()
            pd.DataFrame({"epoch": [0, 0], "label": [0, 2]}).to_csv(
                raw / "labels" / "abc_labeled_sleep.csv", index=False
            )
            pd.DataFrame({
                "subject_id": ["abc"], "epoch_idx": [0], "t_rel_sec": [0],
                "activity_mean": [1.0], "activity_std": [0.1], "activity_n": [10],
                "hr_mean": [60.0], "hr_std": [1.0], "hr_n": [2],
            }).to_csv(cache / "abc_unified_30s.csv", index=False)
            with self.assertRaisesRegex(ValueError, "duplicate raw PSG epochs"):
                build_native_dataset(raw, cache)

    def test_fractional_stage_and_infinite_sensor_fail_closed(self):
        for bad_stage, bad_activity, message in [(1.5, 1.0, "stages must be integers"), (1, np.inf, "non-finite")]:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                raw = root / "raw"
                cache = root / "cache"
                (raw / "labels").mkdir(parents=True)
                cache.mkdir()
                pd.DataFrame({"epoch": [0], "label": [bad_stage]}).to_csv(
                    raw / "labels" / "abc_labeled_sleep.csv", index=False
                )
                pd.DataFrame({
                    "subject_id": ["abc"], "epoch_idx": [0], "t_rel_sec": [0],
                    "activity_mean": [bad_activity], "activity_std": [0.1], "activity_n": [10],
                    "hr_mean": [60.0], "hr_std": [1.0], "hr_n": [2],
                }).to_csv(cache / "abc_unified_30s.csv", index=False)
                with self.assertRaisesRegex(ValueError, message):
                    build_native_dataset(raw, cache)

    def test_coarse_bins_require_complete_contiguous_native_support(self):
        rows = []
        for epoch_idx in range(5):
            rows.append({
                "subject_id": "abc", "epoch_idx": epoch_idx,
                "is_sleep": int(epoch_idx > 0),
                "activity_mean": float(epoch_idx), "activity_std": 0.1, "activity_n": 10,
                "hr_mean": 60.0 + epoch_idx, "hr_std": 1.0, "hr_n": 2,
            })
        native = pd.DataFrame(rows)
        native.loc[native["epoch_idx"] == 3, "hr_mean"] = np.nan
        aggregate, projection = aggregate_epoch_dataset(native, 60)
        self.assertEqual(aggregate["bin_idx"].tolist(), [0])
        self.assertEqual(projection["epoch_idx"].tolist(), [0, 1])
        self.assertEqual(aggregate["is_sleep"].tolist(), [1])
        self.assertEqual(len(projection), 2)
        self.assertEqual(
            set(AGG_ACTIVITY_FEATURES + AGG_HR_FEATURES).issubset(aggregate.columns), True
        )

    def test_winner_uses_validation_metric_with_fixed_tie_break(self):
        rows = [
            {"model": "dummy", "epoch_seconds": 30,
             "metrics": {"subject_macro_balanced_accuracy": 0.60}},
            {"model": "activity_logistic", "epoch_seconds": 300,
             "metrics": {"subject_macro_balanced_accuracy": 0.70}},
            {"model": "activity_logistic", "epoch_seconds": 60,
             "metrics": {"subject_macro_balanced_accuracy": 0.70}},
            {"model": "hist_gradient_boosting", "epoch_seconds": 30,
             "metrics": {"subject_macro_balanced_accuracy": 0.70}},
        ]
        winner = choose_winner(rows)
        self.assertEqual(winner["model"], "activity_logistic")
        self.assertEqual(winner["epoch_seconds"], 60)

    def test_all_resolutions_can_be_restricted_to_same_native_support(self):
        projection_30 = pd.DataFrame({
            "subject_id": ["a", "a", "a"], "epoch_idx": [0, 1, 2],
            "bin_idx": [0, 1, 2], "y_true": [0, 1, 1],
        })
        projection_60 = pd.DataFrame({
            "subject_id": ["a", "a"], "epoch_idx": [0, 1],
            "bin_idx": [0, 0], "y_true": [0, 1],
        })
        placeholder = pd.DataFrame()
        prepared = {30: (placeholder, projection_30), 60: (placeholder, projection_60)}
        support = common_native_support(prepared)
        first = restrict_projection(projection_30, support)
        second = restrict_projection(projection_60, support)
        self.assertEqual(first[["subject_id", "epoch_idx"]].values.tolist(), [["a", 0], ["a", 1]])
        self.assertEqual(
            first[["subject_id", "epoch_idx"]].values.tolist(),
            second[["subject_id", "epoch_idx"]].values.tolist(),
        )

    def test_metrics_and_paired_subject_bootstrap_report_required_fields(self):
        rows = []
        for subject_id in ["a", "b"]:
            for y_true, y_pred in [(0, 0), (0, 1), (1, 1), (1, 1)]:
                rows.append({
                    "subject_id": subject_id, "y_true": y_true, "y_pred": y_pred,
                    "probability_sleep": 0.8 if y_pred else 0.2,
                })
        winner = pd.DataFrame(rows)
        dummy = winner.copy()
        dummy["y_pred"] = 1
        dummy["probability_sleep"] = 0.75
        result = metrics(winner)
        self.assertIn("subject_macro_f1", result)
        self.assertIn("wake_specificity", result)
        self.assertIn("sleep_sensitivity", result)
        self.assertEqual(result["confusion"], {
            "wake_as_wake_tn": 2, "wake_as_sleep_fp": 2,
            "sleep_as_wake_fn": 0, "sleep_as_sleep_tp": 4,
        })
        bootstrap = paired_subject_bootstrap(winner, dummy, iterations=100)
        self.assertEqual(bootstrap["subjects"], 2)
        self.assertEqual(bootstrap["iterations"], 100)

    def test_hgb_is_fixed_iteration_and_existing_output_is_refused(self):
        model, _ = make_model("hist_gradient_boosting")
        self.assertFalse(model.early_stopping)
        self.assertEqual(model.max_iter, 60)
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp)
            with self.assertRaises(FileExistsError):
                run_experiments(Path("missing"), Path("missing"), existing)


if __name__ == "__main__":
    unittest.main()
