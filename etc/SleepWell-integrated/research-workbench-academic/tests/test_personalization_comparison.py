from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from researchbench import personalization_comparison as pc


def episode_frame(people=("A",), nights=3):
    rows = []
    for person_number, person in enumerate(people):
        for day in range(nights):
            cutoff = pd.Timestamp("2026-01-01T20:00:00+09:00") + pd.Timedelta(days=day)
            onset = 180.0 + 10 * person_number
            wake = 660.0 + 10 * person_number
            bed = cutoff + pd.Timedelta(minutes=onset)
            wake_time = cutoff + pd.Timedelta(minutes=wake)
            rows.append({
                "cohort": "CMI" if person_number % 2 == 0 else "MESA",
                "subject_id": person,
                "split": "train",
                "cutoff": cutoff.isoformat(),
                "bed": bed.isoformat(),
                "wake": wake_time.isoformat(),
                "observed_at": wake_time.isoformat(),
                "bedtime_min": onset,
                "wake_min": wake,
                "duration_min": wake - onset,
            })
    return pd.DataFrame(rows)


def prior():
    return {"bed_offset": 180.0, "wake_offset": 660.0}


class IdentityTransform:
    def transform(self, values):
        return np.asarray(values, float)


class PairEstimator:
    def predict(self, values):
        values = np.asarray(values, float)
        return values[:, :2]


class PersonalizationComparisonTests(unittest.TestCase):
    def test_vectorized_physics_candidates_match_original_solver(self):
        frame = episode_frame(nights=4)
        features, _, _, _ = pc.replay_features(frame, prior())
        predicted, fallback, offsets = pc.physics_candidates(features)
        core = pc.pt.vendor_module("night_core")
        self.assertEqual(predicted.shape, (len(frame), 27, 2))
        np.testing.assert_array_equal(offsets[0], [0.0, 0.0, 0.0])
        for row_number, row in enumerate(features.itertuples()):
            phase = (1200.0 + (row.habit_bed + row.habit_wake) / 2.0) % 1440.0
            for candidate_number, (phase_delta, high_delta, low_delta) in enumerate(offsets):
                expected = core.physical_prediction(
                    row.current_s,
                    core.local_time(frame.iloc[row_number].cutoff),
                    phase + phase_delta,
                    row.sleep_threshold + high_delta,
                    row.wake_threshold + low_delta,
                    (row.habit_bed, row.habit_wake),
                )
                np.testing.assert_allclose(
                    predicted[row_number, candidate_number], expected[:2], atol=1e-10, rtol=0
                )
                self.assertEqual(bool(fallback[row_number, candidate_number]), bool(expected[2]))

    def test_zero_history_uses_unmodified_physics_features(self):
        frame = episode_frame(("A", "B"), nights=1)
        normal, personal, counts, diagnostics = pc.replay_features(frame, prior())
        np.testing.assert_array_equal(counts, [0, 0])
        self.assertTrue(diagnostics.physics_candidate.eq(0).all())
        columns = ["current_s", "current_c", "sleep_threshold", "wake_threshold",
                   "physical_bed", "physical_wake"]
        np.testing.assert_allclose(personal[columns], normal[columns], atol=1e-12, rtol=0)

    def test_physics_selection_uses_only_prior_available_outcomes(self):
        frame = episode_frame(nights=3)
        frame[["bedtime_min", "wake_min"]] = [900.0, 1380.0]
        features, _, _ = pc.pt.replay_features(frame, prior())
        predictions = np.zeros((len(frame), len(pc.OFFSETS), 2), float)
        predictions[:] = [0.0, 480.0]
        predictions[:, 1] = [900.0, 1380.0]
        candidates = predictions, np.zeros((len(frame), len(pc.OFFSETS)), bool), pc.OFFSETS.copy()

        _, diagnostics = pc.physics_features(frame, features, candidates)
        self.assertEqual(diagnostics.physics_candidate.tolist(), [0, 1, 1])

        changed_current = frame.copy()
        changed_current.loc[1, ["bedtime_min", "wake_min"]] = [0.0, 480.0]
        _, changed = pc.physics_features(changed_current, features, candidates)
        self.assertEqual(changed.loc[1, "physics_candidate"], diagnostics.loc[1, "physics_candidate"])
        self.assertNotEqual(changed.loc[2, "physics_candidate"], diagnostics.loc[2, "physics_candidate"])

        unavailable = frame.copy()
        unavailable.loc[0, "observed_at"] = (
            pd.Timestamp(unavailable.loc[1, "cutoff"]) + pd.Timedelta(hours=1)
        ).isoformat()
        _, unavailable_diagnostics = pc.physics_features(unavailable, features, candidates)
        self.assertEqual(unavailable_diagnostics.loc[1, "physics_eligible_count"], 0)
        self.assertEqual(unavailable_diagnostics.loc[1, "physics_candidate"], 0)

    def test_residual_correction_is_nonrecursive_and_causal(self):
        frame = episode_frame(nights=3)
        frame[["bedtime_min", "wake_min"]] = [200.0, 680.0]
        base = np.tile([100.0, 580.0], (len(frame), 1))
        corrected, diagnostics = pc.residual_predictions(frame, base)
        module = pc.pt.vendor_module("adaptive_personalization")
        history = []
        expected = []
        for row in frame.itertuples():
            adjustment = module.compute_adjustment(history, row.cutoff)
            expected.append([adjustment["onset_correction_min"], adjustment["wake_correction_min"]])
            history.append({
                "night_id": row.cutoff,
                "outcome_at": row.wake,
                "available_at": row.observed_at,
                "onset_residual_min": row.bedtime_min - 100.0,
                "wake_residual_min": row.wake_min - 580.0,
            })
        expected = np.asarray(expected)
        np.testing.assert_allclose(
            diagnostics[["onset_correction_min", "wake_correction_min"]], expected
        )
        np.testing.assert_allclose(corrected, base + expected)

        changed = frame.copy()
        changed.loc[1, ["bedtime_min", "wake_min"]] = [900.0, 1400.0]
        _, changed_diagnostics = pc.residual_predictions(changed, base)
        np.testing.assert_allclose(changed_diagnostics.loc[1], diagnostics.loc[1])
        self.assertFalse(np.allclose(changed_diagnostics.loc[2], diagnostics.loc[2]))

    def test_personalization_state_never_crosses_people(self):
        frame = episode_frame(("A", "B"), nights=2).sort_values("cutoff", kind="stable").reset_index(drop=True)
        base = np.tile([180.0, 660.0], (len(frame), 1))
        frame.loc[frame.subject_id.eq("B"), ["bedtime_min", "wake_min"]] = [900.0, 1400.0]
        _, diagnostics = pc.residual_predictions(frame, base)
        second_a = frame.index[(frame.subject_id.eq("A"))][-1]
        second_b = frame.index[(frame.subject_id.eq("B"))][-1]
        self.assertEqual(diagnostics.loc[second_a, "residual_eligible_count"], 1)
        self.assertEqual(diagnostics.loc[second_a, "onset_correction_min"], 0.0)
        self.assertGreater(diagnostics.loc[second_b, "onset_correction_min"], 0.0)

    def test_fold_fit_rejects_held_rows_and_never_sees_held_sentinel(self):
        frame = episode_frame(tuple(f"P{i}" for i in range(10)), nights=2)
        # Ensure both cohorts have enough people for a two-fold split.
        assignments = pc.rv.fold_assignments(frame, 2, 1, 42)
        train, held = pc.rv.fold_frames(frame, assignments, 0, 0)
        held = held.copy()
        held[["bedtime_min", "wake_min"]] = 1e12
        seen = []

        def replay_only_train(value, prior_value, progress=None, cancelled=None):
            seen.extend(value.subject_id.unique())
            self.assertLess(value[["bedtime_min", "wake_min"]].to_numpy().max(), 1e12)
            names = pc.cc.feature_names("hybrid")
            data = pd.DataFrame(np.arange(len(value) * len(names), dtype=float).reshape(len(value), -1),
                                index=value.index, columns=names)
            # Avoid constant columns so the real weighted preprocessor is exercised.
            data += np.arange(len(value), dtype=float)[:, None]
            return data, data.copy(), [0] * len(value), pd.DataFrame(index=value.index)

        class Estimator:
            def fit(self, x, y, sample_weight=None):
                self.n_rows = len(x)
                return self

        class Pipeline:
            named_steps = {"model": Estimator()}

        with patch.object(pc, "replay_features", side_effect=replay_only_train), \
                patch.object(pc.pt, "make_model", return_value=Pipeline()):
            bundle = pc.fit_fold(train, 42)
        self.assertEqual(set(seen), set(train.subject_id))
        self.assertTrue(set(pc.FIT_MODELS).issubset(bundle))
        with self.assertRaisesRegex(ValueError, "fold-training"):
            pc.fit_fold(held, 42)

    def test_actual_four_arm_evaluation_cannot_use_current_or_future_targets(self):
        frame = episode_frame(nights=3)
        bundles = {"prior": prior()}
        for model, features in {
            "data33": ["habit_bed", "habit_wake"],
            "hybrid39": ["physical_bed", "physical_wake"],
            "hybrid39_personalized": ["physical_bed", "physical_wake"],
        }.items():
            bundles[model] = {"features": features, "preprocessor": IdentityTransform(),
                              "model": PairEstimator()}
        expected, _, _, _ = pc.evaluate_fold(frame, bundles)

        changed = frame.copy()
        changed.loc[1:, ["bedtime_min", "wake_min"]] = [[900.0, 1400.0], [0.0, 30.0]]
        actual, _, _, _ = pc.evaluate_fold(changed, bundles)
        for model in pc.MODELS:
            np.testing.assert_allclose(actual[model][:2], expected[model][:2], atol=1e-12, rtol=0)
        # The changed second-night outcome becomes available only for the third prediction.
        self.assertFalse(np.allclose(actual["data33_residual"][2], expected["data33_residual"][2]))

        future_only = frame.copy()
        future_only.loc[2, ["bedtime_min", "wake_min"]] = [0.0, 30.0]
        future_predictions, _, _, _ = pc.evaluate_fold(future_only, bundles)
        for model in pc.MODELS:
            np.testing.assert_allclose(future_predictions[model], expected[model], atol=1e-12, rtol=0)

    def test_procedure_lock_detects_database_and_partition_changes(self):
        frame = episode_frame(tuple(f"P{i}" for i in range(10)), nights=1)
        manifest = {"summary": [], "time_contract": {}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "database.sqlite3"
            database.write_bytes(b"fixed")
            config = {"database_path": str(database), "output_dir": str(root / "run"),
                      "cv_folds": 2, "cv_repeats": 1, "bootstrap_replicates": 100}
            with patch.object(pc.pt, "load_database",
                              return_value=(frame, manifest, pc.pt.sha256(database))):
                pc.prepare_comparison(config)
            pc.verify_lock(config)
            assignment = root / "run" / "private_fold_assignments.csv"
            assignment.write_text(assignment.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Procedure lock changed"):
                pc.verify_lock(config)

    def test_presentation_rule_honors_all_pass_fail_borders(self):
        values = {
            "data33": {"CMI": 10.0, "MESA": 10.0, "MACRO": 10.0},
            # Exactly 1 minute better, exactly 1 minute worse in one cohort.
            "data33_residual": {"CMI": 11.0, "MESA": 7.0, "MACRO": 9.0},
            "hybrid39": {"CMI": 9.0, "MESA": 9.0, "MACRO": 9.0},
            "hybrid39_personalized": {"CMI": 9.01, "MESA": 9.01, "MACRO": 9.01},
        }
        metrics = [{"model": model, "cohort": cohort, "joint_mae_min": score}
                   for model, cohorts in values.items() for cohort, score in cohorts.items()]
        paired = []
        for model, lower in (("data33_residual", 1e-9), ("hybrid39", 0.0),
                             ("hybrid39_personalized", 1.0)):
            paired.append({"reference": "data33", "model": model, "cohort": "MACRO",
                           "ci95_low": lower})
        cold = {model: (11.0 if model == "data33_residual" else 10.0) for model in pc.MODELS}
        history = [{"model": model, "cohort": "MACRO", "history_band": "0",
                    "joint_mae_min": score} for model, score in cold.items()]
        selected = pc.recommendation(metrics, paired, history)
        self.assertEqual(selected["recommended_model"], "data33_residual")
        eligible = {row["model"]: row["eligible"] for row in selected["candidates"]}
        self.assertTrue(eligible["data33_residual"])
        self.assertFalse(eligible["hybrid39"])
        self.assertFalse(eligible["hybrid39_personalized"])

        paired[0]["ci95_low"] = 0.0
        self.assertEqual(pc.recommendation(metrics, paired, history)["recommended_model"], "data33")

    def test_all_models_are_locked_before_four_arm_oof_evaluation(self):
        frame = episode_frame(tuple(f"P{i}" for i in range(10)), nights=2)
        manifest = {"summary": [], "time_contract": {}}
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "database.sqlite3"
            database.write_bytes(b"fixed")
            config = {"database_path": str(database), "output_dir": str(root / "run"),
                      "cv_folds": 2, "cv_repeats": 1, "bootstrap_replicates": 100}

            def fake_fit(train, seed, progress=None, cancelled=None):
                self.assertEqual(set(train.split), {"train"})
                events.append(("fit", len(train)))
                return {"train_rows": len(train)}

            def fake_evaluate(held, bundle, progress=None, cancelled=None):
                model_lock = root / "run" / "all_fold_models_lock.json"
                self.assertTrue(model_lock.is_file())
                self.assertEqual(len(pc.json.loads(model_lock.read_text(encoding="utf-8"))["models_sha256"]), 3)
                self.assertEqual(len([event for event in events if event[0] == "fit"]), 3)
                events.append(("evaluate", len(held)))
                targets = held[pc.pt.TARGETS].to_numpy(float)
                predictions = {model: targets + float(number + 1)
                               for number, model in enumerate(pc.MODELS)}
                diagnostics = pd.DataFrame({
                    "physics_candidate": np.zeros(len(held), int),
                    "physics_fallback": np.zeros(len(held), bool),
                    "physics_zero_fallback": np.zeros(len(held), bool),
                    "physics_eligible_count": np.zeros(len(held), int),
                    "physics_decayed_mass": np.zeros(len(held)),
                    "residual_eligible_count": np.zeros(len(held), int),
                    "onset_correction_min": np.zeros(len(held)),
                    "wake_correction_min": np.zeros(len(held)),
                }, index=held.index)
                return predictions, [0] * len(held), np.zeros(len(held), int), diagnostics

            with patch.object(pc.pt, "load_database",
                              return_value=(frame, manifest, pc.pt.sha256(database))):
                pc.prepare_comparison(config)
                with patch.object(pc, "fit_fold", side_effect=fake_fit), \
                        patch.object(pc, "evaluate_fold", side_effect=fake_evaluate):
                    result = pc.run_comparison(config)

            self.assertEqual([event[0] for event in events],
                             ["fit", "fit", "fit", "evaluate", "evaluate"])
            self.assertEqual(events[2][1], len(frame))
            self.assertEqual(len(result["summary"]), 12)
            private = pd.read_csv(root / "run" / "private_oof_predictions.csv")
            self.assertEqual(set(private.model), set(pc.MODELS))
            for number, model in enumerate(pc.MODELS):
                part = private.loc[private.model.eq(model)]
                np.testing.assert_allclose(
                    part.predicted_onset_min - part.actual_onset_min, float(number + 1)
                )
            with self.assertRaisesRegex(FileExistsError, "already started"):
                pc.run_comparison(config)


if __name__ == "__main__":
    unittest.main()
