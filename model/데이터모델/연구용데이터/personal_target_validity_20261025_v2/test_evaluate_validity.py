from __future__ import annotations

import math
import statistics
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import evaluate_validity as validity


KST = timezone(timedelta(hours=9))
WORKSPACE = Path(__file__).resolve().parents[3]


def target(
    target_id: str,
    split: str,
    cutoff: datetime,
    onset: float,
    wake: float,
    *,
    participant_id: str = "P01",
    observed_at: datetime | None = None,
):
    sleep_start = cutoff + timedelta(minutes=onset)
    sleep_end = cutoff + timedelta(minutes=wake)
    return {
        "participant_id": participant_id,
        "target_id": target_id,
        "split": split,
        "cutoff": cutoff.isoformat(),
        "sleep_start": sleep_start.isoformat(),
        "sleep_end": sleep_end.isoformat(),
        "observed_at": (observed_at or sleep_end).isoformat(),
        "actual_onset_offset_min": onset,
        "actual_wake_offset_min": wake,
        "actual_interval_min": wake - onset,
    }


class ExistingHelperCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = validity.load_helpers(validity.paths_for(WORKSPACE)["target_validity_helpers"])


class HistoryCutoffTests(ExistingHelperCase):
    def test_complete_cross_split_history_still_requires_strict_wake_and_observation_cutoffs(self):
        test_cutoff = datetime(2026, 10, 4, 20, tzinfo=KST)
        rows = [
            target("train-ok", "train", datetime(2026, 10, 1, 20, tzinfo=KST), 180, 660),
            target("validation-ok", "validation", datetime(2026, 10, 2, 20, tzinfo=KST), 300, 700),
            target(
                "observed-at-cutoff",
                "validation",
                datetime(2026, 10, 3, 20, tzinfo=KST),
                200,
                650,
                observed_at=test_cutoff,
            ),
            target("wake-at-cutoff", "validation", datetime(2026, 10, 3, 20, tzinfo=KST), 960, 1440),
            target("heldout", "test", test_cutoff, 240, 720),
        ]
        predictions = validity.calendar_test(
            rows, {"P01": {"bed_offset": 180.0, "wake_offset": 660.0}}, self.helper
        )
        reference = next(row for row in predictions if row["arm"] == validity.REFERENCE_ARM)
        self.assertEqual(reference["complete_pre_cutoff_history_n"], 2)
        self.assertEqual(reference["recent_14_calendar_day_history_n"], 2)
        self.assertAlmostEqual(reference["predicted_onset_offset_min"], 240.0)
        self.assertAlmostEqual(reference["predicted_wake_offset_min"], 680.0)


class CalendarVersusObservationWindowTests(ExistingHelperCase):
    def test_seven_calendar_days_is_not_last_seven_observations(self):
        cutoff = datetime(2026, 10, 31, 20, tzinfo=KST)
        ages = [12, 11, 10, 9, 8, 3, 2]
        onsets = [100, 110, 120, 130, 140, 300, 500]
        records = []
        for index, (age, onset) in enumerate(zip(ages, onsets)):
            historical_cutoff = cutoff - timedelta(days=age)
            records.append(
                {
                    "id": f"h{index}",
                    "source": "research",
                    "bed": (historical_cutoff + timedelta(minutes=onset)).isoformat(),
                    "wake": (historical_cutoff + timedelta(minutes=onset + 480)).isoformat(),
                    "observed_at": (historical_cutoff + timedelta(minutes=onset + 480)).isoformat(),
                }
            )
        estimates, diagnostics = self.helper.baseline_predictions(
            records,
            cutoff.isoformat(),
            {"bed_offset": 200.0, "wake_offset": 680.0},
            (cutoff - timedelta(days=20)).isoformat(),
        )
        last_seven_observation_median = statistics.median(onsets)
        self.assertEqual(diagnostics["prior_records"], 7)
        self.assertAlmostEqual(estimates["mean7"][0], 400.0)
        self.assertAlmostEqual(last_seven_observation_median, 130.0)
        self.assertNotEqual(estimates["mean7"][0], last_seven_observation_median)
        self.assertNotEqual(validity.CALENDAR_ARMS["mean7"], validity.LEGACY_RECENT7_ARM)
        self.assertIn("not weekday/weekend", validity.NEW_ARM_META["baseline_exact_weekday_28d"]["description"])


class ConstantAndShortSeriesTests(ExistingHelperCase):
    def test_constant_targets_are_excluded_from_centered_diagnostic_and_flagged(self):
        base = datetime(2026, 10, 1, 20, tzinfo=KST)
        targets = [target(f"p1-{day}", "test", base + timedelta(days=day), 200, 700) for day in range(3)]
        prior = {"bed_offset": 200.0, "wake_offset": 700.0}
        predictions = [
            validity.prediction(row, "candidate", 210 + index, 690 - index, prior, "test", index)
            for index, row in enumerate(targets)
        ]
        diagnostic = validity.centered_diagnostics(predictions, self.helper)["candidate"]
        flags = validity.target_flags(targets)["P01"]
        self.assertEqual(diagnostic["onset"]["eligible_people"], 0)
        self.assertEqual(diagnostic["wake"]["eligible_people"], 0)
        self.assertEqual(diagnostic["interval"]["eligible_people"], 0)
        self.assertTrue(flags["constant_onset_target"])
        self.assertTrue(flags["constant_wake_target"])
        self.assertTrue(flags["constant_interval_target"])

    def test_two_nights_receive_short_series_flag(self):
        base = datetime(2026, 10, 1, 20, tzinfo=KST)
        rows = [
            target("p2-a", "test", base, 200, 700, participant_id="P02"),
            target("p2-b", "test", base + timedelta(days=1), 220, 750, participant_id="P02"),
        ]
        self.assertTrue(validity.target_flags(rows)["P02"]["short_for_centered_evaluation"])


class PairedSameNightTests(unittest.TestCase):
    @staticmethod
    def row(target_id: str, arm: str, error: float):
        return {
            "participant_id": "P01",
            "target_id": target_id,
            "arm": arm,
            "actual_onset_offset_min": 200.0,
            "actual_wake_offset_min": 700.0,
            "actual_interval_min": 500.0,
            "onset_abs_error_min": error,
            "wake_abs_error_min": error,
            "interval_abs_error_min": error,
            "joint_abs_error_min": error,
        }

    def test_pairing_uses_ids_not_row_order_or_unmatched_nights(self):
        rows = [
            self.row("night-a", validity.REFERENCE_ARM, 10.0),
            self.row("night-b", validity.REFERENCE_ARM, 20.0),
            self.row("night-b", "candidate", 5.0),
            self.row("night-a", "candidate", 5.0),
            self.row("candidate-only", "candidate", 0.0),
        ]
        result = validity.paired_skill(rows)["candidate"]
        onset = result["metrics"]["onset"]
        self.assertEqual(result["n_pairs"], 2)
        self.assertEqual(result["candidate_only_n"], 1)
        self.assertEqual(result["reference_only_n"], 0)
        self.assertAlmostEqual(onset["candidate_macro_equal_person_mae_min"], 5.0)
        self.assertAlmostEqual(onset["reference_macro_equal_person_mae_min"], 15.0)
        self.assertTrue(math.isclose(onset["skill_one_minus_candidate_over_reference"], 2 / 3))


class FeasibilityGateTests(unittest.TestCase):
    def test_zero_strict_links_stops_before_fit_without_null_effect_claim(self):
        result = validity.survey_gate(
            {"survey_incorporation": {"strict_watch_join_rows": 0}},
            {"strictly_available_linked_survey_nights": 0},
        )
        self.assertFalse(result["gate_passed"])
        self.assertFalse(result["survey_model_fit_attempted"])
        self.assertFalse(result["null_effect_claimed"])
        self.assertIsNone(result["effect_estimate"])
        self.assertIn("non-estimable", result["interpretation"])


class OutputSafetyTests(unittest.TestCase):
    def test_existing_registered_result_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / validity.RESULTS[0]).write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                validity.ensure_absent(output)


if __name__ == "__main__":
    unittest.main()
