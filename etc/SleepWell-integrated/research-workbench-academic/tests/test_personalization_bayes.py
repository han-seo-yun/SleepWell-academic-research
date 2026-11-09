import copy
import importlib.util
import json
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "research_workbench_academic" / "scripts" / "compare_personalization_bayes.py"
SPEC = importlib.util.spec_from_file_location("test_personalization_bayes_module", SCRIPT)
assert SPEC and SPEC.loader
BAYES = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BAYES
SPEC.loader.exec_module(BAYES)


class PersonalizationBayesTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 31, 20, tzinfo=timezone.utc)

    def history_row(self, night, outcome_days_ago, available_days_ago=None,
                    onset=10.0, wake=-5.0, base_bed=1380.0, base_wake=420.0):
        if available_days_ago is None:
            available_days_ago = outcome_days_ago
        return {
            "night_id": night,
            "outcome_at": (self.now - timedelta(days=outcome_days_ago)).isoformat(),
            "available_at": (self.now - timedelta(days=available_days_ago)).isoformat(),
            "onset_residual_min": onset,
            "wake_residual_min": wake,
            "base_bed": base_bed,
            "base_wake": base_wake,
        }

    def complete_train(self, residual=0.0):
        rows = []
        for cohort in BAYES.COHORTS:
            for arm in BAYES.ARMS:
                for person_index, person in enumerate(("p1", "p2")):
                    for night in range(2):
                        base = 1300.0 + 100.0 * person_index + 20.0 * night
                        rows.append({"split": "train", "cohort": cohort, "arm": arm,
                            "participant_id": person, "base_bed": base, "base_wake": base + 480.0,
                            "actual_bed": base + residual,
                            "actual_wake": base + 480.0 + residual})
        return rows

    def test_closed_form_empirical_bayes(self):
        posterior, variance = BAYES.eb_mean(10.0, 4.0, 9.0, [13.0, 7.0], [1.0, 1.0])
        precision = 1 / 4 + 2 / 9
        self.assertAlmostEqual(posterior, (10 / 4 + 20 / 9) / precision)
        self.assertAlmostEqual(variance, 1 / precision)

    def test_random_walk_gap_increases_variance_and_ou_reverts(self):
        row = BAYES.latest_eligible([self.history_row("n1", 14)], self.now)[0]
        near = row["outcome"] + timedelta(days=1)
        far = row["outcome"] + timedelta(days=14)
        rw_near = BAYES.kalman_mean(0.0, 100.0, 25.0, [row], "onset", near,
                                    mean_reverting=False)
        rw_far = BAYES.kalman_mean(0.0, 100.0, 25.0, [row], "onset", far,
                                   mean_reverting=False)
        self.assertGreater(rw_far[1], rw_near[1])
        ou_near = BAYES.kalman_mean(0.0, 100.0, 25.0, [row], "onset", near,
                                    mean_reverting=True)
        ou_far = BAYES.kalman_mean(0.0, 100.0, 25.0, [row], "onset", far,
                                   mean_reverting=True)
        self.assertLess(abs(ou_far[0]), abs(ou_near[0]))
        self.assertGreater(ou_far[1], ou_near[1])
        self.assertLessEqual(ou_far[1], 100.0)

    def test_availability_causality_and_latest_revision(self):
        old = self.history_row("same", 5, 4, onset=10.0)
        revised = self.history_row("same", 5, 2, onset=20.0)
        future = self.history_row("future", 1, -1, onset=99.0)
        impossible = self.history_row("impossible", 1, 2, onset=99.0)
        rows = BAYES.latest_eligible([old, revised, future, impossible], self.now)
        self.assertEqual([row["night_id"] for row in rows], ["same"])
        self.assertEqual(rows[0]["onset"], 20.0)

    def test_affine_posterior_is_finite_under_sparse_small_variance(self):
        h = {"T_min2": 1.0, "R_min2": 1.0, "affine_beta0_min": 0.0,
             "affine_beta1_per_sd": 0.0, "base_centre_min": 1380.0,
             "base_scale_min": 60.0}
        rows = BAYES.latest_eligible([self.history_row("n1", 1, onset=2.0)], self.now)
        values = BAYES.affine_individual(h, rows, "onset", self.now, 1400.0)
        self.assertTrue(all(math.isfinite(value) for value in values))
        self.assertGreater(values[-1], 0.0)

    def test_train_fit_zero_residual_affine_identity_and_fallback_flags(self):
        fitted = BAYES.fit_hyperparameters(self.complete_train(residual=0.0))
        self.assertEqual(len(fitted), 8)
        for h in fitted.values():
            self.assertAlmostEqual(h["affine_beta0_min"], 0.0)
            self.assertAlmostEqual(h["affine_beta1_per_sd"], 0.0)
            self.assertEqual(h["R_min2"], 1.0)
            self.assertTrue(h["R_floor_applied"])
            self.assertFalse(h["R_no_within_variance_fallback"])

    def test_train_only_guard_rejects_other_split(self):
        rows = self.complete_train()
        rows[0]["split"] = "validation"
        with self.assertRaisesRegex(ValueError, "Train-only"):
            BAYES.fit_hyperparameters(rows)

    def test_no_within_participant_variance_uses_flagged_floor(self):
        rows = [row for row in self.complete_train() if row["base_bed"] % 100 == 0]
        fitted = BAYES.fit_hyperparameters(rows)
        for h in fitted.values():
            self.assertEqual(h["R_min2"], 1.0)
            self.assertTrue(h["R_no_within_variance_fallback"])

    def test_protocol_formula_and_gate_tampering_rejected(self):
        protocol = json.loads(BAYES.DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
        BAYES._protocol_matches(protocol)
        formula = copy.deepcopy(protocol)
        formula["candidate_formulas"]["kalman_random_walk_q01"] = "Q=0"
        with self.assertRaisesRegex(ValueError, "formula"):
            BAYES._protocol_matches(formula)
        gate = copy.deepcopy(protocol)
        gate["locked_test_gate"]["seed"] += 1
        with self.assertRaisesRegex(ValueError, "seed"):
            BAYES._protocol_matches(gate)

    def test_selection_uses_validation_only(self):
        records = []
        for split in ("validation", "test"):
            for cohort in BAYES.COHORTS:
                for arm in BAYES.ARMS:
                    for setting in (BAYES.CURRENT, *BAYES.CANDIDATES):
                        error = 10.0
                        if split == "validation" and setting == BAYES.CANDIDATES[0]:
                            error = 5.0
                        if split == "test" and setting == BAYES.CANDIDATES[-1]:
                            error = 0.0
                        records.append({"split": split, "cohort": cohort, "arm": arm,
                            "setting": setting, "participant_id": "p", "joint_error": error,
                            "cap_hits": 0})
        selected, _, passed = BAYES.choose_candidate(records)
        self.assertEqual(selected, BAYES.CANDIDATES[0])
        self.assertTrue(passed)


if __name__ == "__main__":
    unittest.main()
