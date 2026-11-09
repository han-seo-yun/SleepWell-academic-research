from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compare_personalization_growth", ROOT / "scripts" / "compare_personalization_growth.py")
growth = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = growth
SPEC.loader.exec_module(growth)
ZONE = timezone(timedelta(hours=9))


def result_row(setting, participant, error, *, cohort="cmi", arm="data", split="validation"):
    return {"cohort": cohort, "participant_id": participant, "split": split, "arm": arm,
            "setting": setting, "joint_error": error, "bed_error": error, "wake_error": error,
            "eligible": 1, "mass": 1.0, "alpha": 0.1, "gap_days": 1.0,
            "cap_hits": 0, "clipped_values": 0, "history_values": 0}


class PersonalizationGrowthTests(unittest.TestCase):
    def test_future_unavailable_and_duplicate_revision_use_production_eligibility(self):
        adaptive, _ = growth.BASE._load_production_modules()
        cutoff = datetime(2026, 1, 10, 20, tzinfo=ZONE)
        history = [
            {"night_id": "same", "outcome_at": (cutoff - timedelta(days=3)).isoformat(),
             "available_at": (cutoff - timedelta(days=2)).isoformat(),
             "onset_residual_min": 10.0, "wake_residual_min": 20.0},
            {"night_id": "same", "outcome_at": (cutoff - timedelta(days=3)).isoformat(),
             "available_at": (cutoff - timedelta(days=1)).isoformat(),
             "onset_residual_min": 40.0, "wake_residual_min": 50.0},
            {"night_id": "future", "outcome_at": (cutoff - timedelta(hours=1)).isoformat(),
             "available_at": (cutoff + timedelta(minutes=1)).isoformat(),
             "onset_residual_min": 999.0, "wake_residual_min": 999.0},
        ]
        values = growth.growth_adjustments(history, cutoff, (growth.CURRENT, *growth.CANDIDATES), adaptive)
        self.assertEqual(values[growth.CURRENT_ID]["eligible"], 1)
        production = adaptive.compute_adjustment(history, cutoff)
        self.assertAlmostEqual(production["raw_onset_residual_mean_min"], 40.0)
        self.assertAlmostEqual(production["raw_wake_residual_mean_min"], 50.0)

    def test_current_baseline_is_exact_production_correction(self):
        adaptive, _ = growth.BASE._load_production_modules()
        cutoff = datetime(2026, 1, 10, 20, tzinfo=ZONE)
        history = [{"night_id": "a", "outcome_at": (cutoff - timedelta(days=2)).isoformat(),
                    "available_at": (cutoff - timedelta(days=1)).isoformat(),
                    "onset_residual_min": 170.0, "wake_residual_min": -150.0}]
        actual = growth.growth_adjustments(history, cutoff, (growth.CURRENT,), adaptive)[growth.CURRENT_ID]
        expected = adaptive.compute_adjustment(history, cutoff)
        self.assertEqual(actual["onset"], expected["onset_correction_min"])
        self.assertEqual(actual["wake"], expected["wake_correction_min"])
        self.assertEqual(actual["alpha"], expected["weight"])

    def test_growth_limits_and_gap_behavior(self):
        n = 365
        fresh_mass = sum(2 ** (-age / 14) for age in range(n))
        gap_mass = fresh_mass * 2 ** (-56 / 14)
        current_fresh = growth.alpha_for(growth.CURRENT, n, fresh_mass)[0]
        current_gap = growth.alpha_for(growth.CURRENT, n, gap_mass)[0]
        self.assertLess(current_fresh, 0.60)
        self.assertLess(current_gap, current_fresh)
        for setting in growth.CANDIDATES[1:]:
            self.assertGreater(growth.alpha_for(setting, n, fresh_mass)[0], current_fresh)
            self.assertLess(growth.alpha_for(setting, n, gap_mass)[0],
                            growth.alpha_for(setting, n, fresh_mass)[0])

    def test_count_weight_ignores_gap_but_others_do_not(self):
        n = 28
        fresh = sum(2 ** (-age / 14) for age in range(n))
        stale = fresh * 2 ** (-56 / 14)
        count = growth.CANDIDATES[0]
        self.assertEqual(growth.alpha_for(count, n, fresh)[0], growth.alpha_for(count, n, stale)[0])
        for setting in (growth.CURRENT, *growth.CANDIDATES[1:]):
            self.assertLess(growth.alpha_for(setting, n, stale)[0],
                            growth.alpha_for(setting, n, fresh)[0])

    def test_reused_nov4_script_matches_recorded_hash(self):
        nov4 = json.loads(growth.NOV4_RESULT.read_text(encoding="utf-8"))
        self.assertEqual(growth.sha256(growth.NOV4_SCRIPT),
                         nov4["provenance"]["comparison_script"]["sha256"])

    def test_selection_uses_validation_only(self):
        rows = []
        for cohort in growth.COHORTS:
            for arm in growth.ARMS:
                participant = f"{cohort}-{arm}"
                rows.append(result_row(growth.CURRENT_ID, participant, 10.0, cohort=cohort, arm=arm))
                for setting in growth.CANDIDATES:
                    error = 8.0 if setting.id == "count_weight" else 10.0
                    rows.append(result_row(setting.id, participant, error, cohort=cohort, arm=arm))
                rows.append(result_row("count_weight", f"test-{participant}", 9999.0,
                                       cohort=cohort, arm=arm, split="test"))
        selected, _, gate = growth.choose_candidate(rows)
        self.assertEqual(selected, "count_weight")
        self.assertTrue(gate)

    def test_protocol_rejects_growth_formula_tamper(self):
        protocol = json.loads(growth.DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
        growth._protocol_matches(protocol)
        protocol["alternatives"][0]["alpha"] = "n/(n+7)"
        with self.assertRaisesRegex(ValueError, "alternatives"):
            growth._protocol_matches(protocol)


if __name__ == "__main__":
    unittest.main()
