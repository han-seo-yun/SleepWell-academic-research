from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import run_experiment as runner


class DummyGate:
    def __init__(self, weight: float) -> None:
        self.weight = float(weight)
        self.calls: list[tuple[np.ndarray, np.ndarray]] = []

    def predict_weights(self, context: pd.DataFrame) -> np.ndarray:
        return np.full(len(context), self.weight)

    def predict(
        self,
        context: pd.DataFrame,
        data_pred: np.ndarray,
        physical_pred: np.ndarray,
    ) -> np.ndarray:
        data = np.asarray(data_pred, dtype=float)
        physical = np.asarray(physical_pred, dtype=float)
        self.calls.append((data.copy(), physical.copy()))
        return runner.blend_predictions(data, physical, self.weight)


def episode_rows(
    cohort: str,
    subjects: list[str],
    nights: int,
    *,
    split: str = "train",
) -> pd.DataFrame:
    rows = []
    row_id = 0
    origin = pd.Timestamp("2026-01-01T20:00:00+09:00")
    for subject_index, subject in enumerate(subjects):
        for night in range(nights):
            cutoff = origin + pd.Timedelta(days=night + subject_index * (nights + 2))
            bed = cutoff + pd.Timedelta(hours=3)
            wake = cutoff + pd.Timedelta(hours=11)
            rows.append(
                {
                    "cohort": cohort,
                    "subject_id": subject,
                    "split": split,
                    "cutoff": cutoff,
                    "bed": bed,
                    "wake": wake,
                    "observed_at": wake,
                    "row_id": row_id,
                    "bed_offset": 180.0 + subject_index,
                    "wake_offset": 660.0 + subject_index,
                }
            )
            row_id += 1
    return pd.DataFrame(rows)


def metric_map(default: float = 100.0) -> dict[str, dict[str, float]]:
    return {arm: {"joint_mae_min": float(default)} for arm in runner.ALL_ARMS}


class ProtocolAndSplitTests(unittest.TestCase):
    def test_protocol_locks_inputs_models_and_exact_18_arms(self) -> None:
        protocol = json.loads((HERE / "protocol.json").read_text(encoding="utf-8"))
        self.assertEqual(tuple(protocol["arms"]), runner.ALL_ARMS)
        self.assertEqual(len(protocol["arms"]), 18)
        self.assertEqual(protocol["seed"], 20261026)
        self.assertEqual(protocol["gate"]["grid"], [0.0, 0.25, 0.5, 0.75, 1.0])
        self.assertEqual(tuple(protocol["gate"]["context_columns"]), runner.CONTEXT_COLUMNS)
        self.assertEqual(protocol["model_families"]["catboost"]["iterations"], 200)
        self.assertEqual(protocol["model_families"]["catboost"]["depth"], 4)
        self.assertEqual(
            protocol["expected_input_sha256"]["v2/cmi/features.parquet"],
            "c98a78567225192cba3604cf359597fb5f7574aabdba8e76a29a1c05df8a06d2",
        )

    def test_group_folds_are_participant_disjoint_and_cover_each_train_row_once(self) -> None:
        frame = episode_rows("cmi", [f"p{i}" for i in range(6)], 2)
        folds, coverage = runner.make_oof_folds(frame, "cmi")
        held_ids: list[int] = []
        for fit, held, audit in folds:
            self.assertFalse(set(fit.subject_id) & set(held.subject_id))
            self.assertTrue(fit.split.eq("train").all())
            self.assertTrue(held.split.eq("validation").all())
            self.assertFalse(audit["strict_availability_purge_applied"])
            held_ids.extend(held.row_id.tolist())
        self.assertCountEqual(held_ids, frame.row_id.tolist())
        self.assertEqual(len(held_ids), len(set(held_ids)))
        self.assertTrue(coverage["all_train_rows_covered"])
        self.assertEqual(coverage["uncovered_train_rows"], 0)

    def test_samsung_folds_are_forward_gap_one_strictly_available_and_reset(self) -> None:
        frame = episode_rows("samsung", ["one"], 16)
        folds, coverage = runner.make_oof_folds(frame, "samsung")
        self.assertEqual(len(folds), 3)
        for fit, held, audit in folds:
            first = held.cutoff.min()
            self.assertLess(fit.cutoff.max(), first)
            self.assertLess(fit.wake.max(), first)
            self.assertLess(fit.observed_at.max(), first)
            self.assertTrue(audit["strict_availability_purge_applied"])
            self.assertTrue(audit["strictly_before_first_held_cutoff"])
            self.assertTrue(held.split.eq("validation").all())
        self.assertFalse(coverage["all_train_rows_covered"])
        self.assertGreater(coverage["uncovered_train_rows"], 0)

    def test_strict_purge_checks_cutoff_wake_and_observed_at(self) -> None:
        first = pd.Timestamp("2026-01-05T20:00:00+09:00")
        candidate = episode_rows("samsung", ["one"], 3)
        candidate.loc[1, "wake"] = first
        candidate.loc[1, "observed_at"] = first
        candidate.loc[2, "observed_at"] = first + pd.Timedelta(minutes=1)
        kept, audit = runner.strict_availability_purge(candidate, first)
        self.assertEqual(kept.row_id.tolist(), [0])
        self.assertEqual(audit["purged_rows"], 2)
        self.assertLess(pd.Timestamp(audit["max_fit_label_availability"]), first)

    def test_fold_prior_rejects_held_rows_and_is_held_label_invariant(self) -> None:
        fit = episode_rows("cmi", ["a", "b"], 2)
        held = episode_rows("cmi", ["held"], 2, split="validation")
        prior = runner.training_prior(fit)
        held.loc[:, ["bed_offset", "wake_offset"]] = [900.0, 1400.0]
        self.assertEqual(prior, runner.training_prior(fit))
        with self.assertRaises(runner.ProtocolError):
            runner.training_prior(pd.concat([fit, held], ignore_index=True))
        self.assertEqual(prior["n_people"], 2)
        self.assertEqual(prior["n_nights"], 4)


class SelectionAndPredictionTests(unittest.TestCase):
    def test_selection_threshold_ties_and_test_independence(self) -> None:
        validation = metric_map()
        validation["baseline_population_median"]["joint_mae_min"] = 90.0
        validation["hgb_gate_depth1"]["joint_mae_min"] = 85.0001
        below = runner.select_validation_arm(validation)
        self.assertEqual(below["selected_arm"], "baseline_population_median")
        self.assertFalse(below["gate_selected"])

        validation["hgb_gate_depth1"]["joint_mae_min"] = 85.0
        validation["catboost_gate_depth1"]["joint_mae_min"] = 85.0
        exact = runner.select_validation_arm(validation)
        self.assertTrue(exact["gate_selected"])
        self.assertEqual(exact["best_gate"], "catboost_gate_depth1")

        unrelated_test_metrics = {arm: -index for index, arm in enumerate(runner.ALL_ARMS)}
        before = runner.select_validation_arm(validation)
        unrelated_test_metrics["hgb_gate_depth2"] = -1_000_000
        after = runner.select_validation_arm(validation)
        self.assertEqual(before, after)

    def test_simpler_nonconditional_wins_a_validation_tie(self) -> None:
        validation = metric_map(120.0)
        validation["baseline_population_median"]["joint_mae_min"] = 80.0
        validation["hgb_data"]["joint_mae_min"] = 80.0
        validation["hgb_gate_depth1"]["joint_mae_min"] = 79.0
        choice = runner.select_validation_arm(validation)
        self.assertEqual(choice["best_nonconditional"], "baseline_population_median")
        self.assertEqual(choice["selected_arm"], "baseline_population_median")

    def test_fixed_and_gate_wiring_use_physical_not_hybrid(self) -> None:
        features = pd.DataFrame({name: [0.0, 0.0] for name in runner.DATA_FEATURES})
        data = np.array([[100.0, 600.0], [120.0, 620.0]])
        hybrid = np.array([[700.0, 1400.0], [710.0, 1410.0]])
        physical = np.array([[200.0, 700.0], [220.0, 720.0]])
        depth1 = DummyGate(0.5)
        depth2 = DummyGate(0.0)
        predictions, weights, diagnostics = runner.predict_family_arms(
            "hgb",
            features,
            data,
            hybrid,
            physical,
            0.25,
            {1: depth1, 2: depth2},
        )
        np.testing.assert_allclose(predictions["hgb_fixed_blend"], data + 0.25 * (physical - data))
        np.testing.assert_allclose(predictions["hgb_gate_depth1"], data + 0.5 * (physical - data))
        np.testing.assert_allclose(predictions["hgb_gate_depth2"], data)
        np.testing.assert_allclose(depth1.calls[0][1], physical)
        self.assertFalse(np.array_equal(depth1.calls[0][1], hybrid))
        np.testing.assert_allclose(weights["hgb_gate_depth2"], 0.0)
        self.assertEqual((diagnostics["gate_depth1_formula_max_abs_error"], runner.baseline_predictions(features.assign(physical_bed=physical[:, 0], physical_wake=physical[:, 1]), np.array([180.0, 660.0]))["physical"].shape), (0.0, (2, 2)))

    def test_identical_night_guard_rejects_missing_arm_rows(self) -> None:
        expected = episode_rows("cmi", ["p"], 2, split="validation")
        arms = ("baseline_recent7", "physical")
        parts = []
        for arm in arms:
            part = expected[["row_id", "subject_id", "cutoff"]].copy()
            part.insert(0, "cohort", "cmi")
            part.insert(1, "split", "validation")
            part.insert(2, "arm", arm)
            parts.append(part)
        table = pd.concat(parts, ignore_index=True)
        audit = runner.assert_identical_nights(table, expected, arms)
        self.assertTrue(audit["identical"])
        with self.assertRaises(runner.ProtocolError):
            runner.assert_identical_nights(table.iloc[:-1], expected, arms)

    def test_samsung_paired_gain_omits_a_false_temporal_ci(self) -> None:
        actual = np.array([[100.0, 600.0], [110.0, 610.0]])
        baseline = actual + 20.0
        changed = actual + 10.0
        result = runner.paired_test_gain(actual, baseline, changed, ["one", "one"], "samsung")
        self.assertIn("confidence interval omitted", result["method"])
        for metric in result["metrics"].values():
            self.assertNotIn("ci95", metric)


class SafetyAndHistoryTests(unittest.TestCase):
    def test_output_directory_is_exclusive_and_preserves_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            existing = Path(temporary) / "existing"
            existing.mkdir()
            sentinel = existing / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                runner.create_output_directory(existing)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_future_outcome_cannot_change_current_or_earlier_features(self) -> None:
        frame = episode_rows("cmi", ["person"], 4, split="validation")
        prior = {"bed_offset": 180.0, "wake_offset": 660.0}
        original, _ = runner.robust_helpers.replay(frame, prior)
        changed = frame.copy()
        changed.loc[2, "bed"] = changed.loc[2, "cutoff"] + pd.Timedelta(hours=6)
        changed.loc[2, "wake"] = changed.loc[2, "cutoff"] + pd.Timedelta(hours=11)
        changed.loc[2, "observed_at"] = changed.loc[2, "wake"]
        perturbed, _ = runner.robust_helpers.replay(changed, prior)
        pd.testing.assert_frame_equal(original.iloc[:3], perturbed.iloc[:3])
        self.assertFalse(
            np.allclose(
                original.loc[3, list(runner.DATA_FEATURES)].to_numpy(float),
                perturbed.loc[3, list(runner.DATA_FEATURES)].to_numpy(float),
            )
        )


if __name__ == "__main__":
    unittest.main()
