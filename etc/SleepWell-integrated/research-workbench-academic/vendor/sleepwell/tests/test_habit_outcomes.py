import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from habit_outcomes import summarize_habits


ZONE = ZoneInfo("Asia/Seoul")


def record(day, bed="23:30", wake="07:00", source="diary", observed_hour=9, **values):
    wake_day = datetime(2026, 1, 1, tzinfo=ZONE) + timedelta(days=day)
    bed_clock = datetime.strptime(bed, "%H:%M").time()
    wake_clock = datetime.strptime(wake, "%H:%M").time()
    bed_day = wake_day - timedelta(days=1) if bed_clock >= wake_clock else wake_day
    item = {
        "bed": f"{bed_day.date().isoformat()}T{bed}:00+09:00",
        "wake": f"{wake_day.date().isoformat()}T{wake}:00+09:00",
        "observed_at": wake_day.replace(hour=observed_hour).isoformat(),
        "source": source,
        "rested": values.get("rested"),
        "fatigue": values.get("fatigue"),
    }
    return item


class HabitOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"created_at": "2026-01-01T00:00:00+09:00"}

    def test_first_and_recent_windows_are_non_overlapping_after_28_days(self):
        rows = [record(day, rested=3, fatigue=6) for day in range(14)]
        rows += [record(day, bed="23:00", wake="06:30", rested=4, fatigue=3) for day in range(28, 42)]
        result = summarize_habits(self.profile, rows, datetime(2026, 2, 11, 23, tzinfo=ZONE))
        self.assertTrue(result["comparison_available"])
        self.assertEqual(result["primary"]["first_14_days"]["recorded_days"], 14)
        self.assertEqual(result["primary"]["recent_14_days"]["recorded_days"], 14)
        self.assertEqual(result["primary"]["descriptive_change_recent_minus_first"]["rested_mean"], 1.0)
        self.assertEqual(result["primary"]["descriptive_change_recent_minus_first"]["fatigue_mean"], -3.0)

    def test_under_28_days_reports_insufficient_without_change(self):
        result = summarize_habits(self.profile, [record(1, rested=4)], datetime(2026, 1, 20, 12, tzinfo=ZONE))
        self.assertEqual(result["status"], "insufficient_followup")
        self.assertFalse(result["comparison_available"])
        self.assertIsNone(result["primary"]["descriptive_change_recent_minus_first"])

    def test_latest_version_per_wake_date_wins_deterministically(self):
        older = record(2, rested=1, fatigue=9, observed_hour=8)
        newer = record(2, rested=5, fatigue=1, observed_hour=10)
        result = summarize_habits(self.profile, [newer, older], datetime(2026, 2, 1, tzinfo=ZONE))
        window = result["primary"]["first_14_days"]
        self.assertEqual(window["recorded_days"], 1)
        self.assertEqual(window["rested_mean"], 5.0)
        self.assertEqual(window["fatigue_mean"], 1.0)

    def test_archive_and_pre_enrollment_are_excluded(self):
        before = record(-1, rested=5)
        archived = record(1, source="archive", rested=5)
        valid = record(2, rested=3)
        result = summarize_habits(self.profile, [before, archived, valid], datetime(2026, 2, 1, tzinfo=ZONE))
        self.assertEqual(result["primary"]["first_14_days"]["recorded_days"], 1)

    def test_wearable_is_auxiliary_and_has_no_subjective_quality(self):
        rows = [record(2, source="health_connect", rested=5, fatigue=0), record(3, source="healthkit")]
        result = summarize_habits(self.profile, rows, datetime(2026, 2, 1, tzinfo=ZONE))
        self.assertEqual(result["primary"]["first_14_days"]["recorded_days"], 0)
        auxiliary = result["auxiliary"]["first_14_days"]
        self.assertEqual(auxiliary["recorded_days"], 2)
        self.assertNotIn("rested_mean", auxiliary)
        self.assertNotIn("fatigue_mean", auxiliary)

    def test_diary_and_wearable_on_same_day_remain_in_separate_strata(self):
        rows = [
            record(2, source="diary", rested=4, fatigue=2, observed_hour=8),
            record(2, source="health_connect", observed_hour=10),
        ]
        result = summarize_habits(self.profile, rows, datetime(2026, 2, 1, tzinfo=ZONE))
        self.assertEqual(result["primary"]["first_14_days"]["recorded_days"], 1)
        self.assertEqual(result["auxiliary"]["first_14_days"]["recorded_days"], 1)
        self.assertEqual(result["primary"]["first_14_days"]["rested_mean"], 4.0)

    def test_auxiliary_coverage_counts_wake_days_not_devices(self):
        rows = []
        for day in range(14):
            rows.extend([
                record(day, source="health_connect", observed_hour=8),
                record(day, source="healthkit", observed_hour=9),
            ])
        result = summarize_habits(self.profile, rows, datetime(2026, 2, 1, tzinfo=ZONE))
        auxiliary = result["auxiliary"]
        self.assertEqual(auxiliary["first_14_days"]["recorded_days"], 14)
        self.assertEqual(auxiliary["first_14_days"]["coverage_rate"], 1.0)
        self.assertEqual(auxiliary["by_source"]["health_connect"]["first_14_days"]["recorded_days"], 14)
        self.assertEqual(auxiliary["by_source"]["healthkit"]["first_14_days"]["recorded_days"], 14)

    def test_circular_regularity_handles_midnight(self):
        rows = [record(1, bed="23:55"), record(2, bed="00:05")]
        result = summarize_habits(self.profile, rows, datetime(2026, 2, 1, tzinfo=ZONE))
        self.assertLess(result["primary"]["first_14_days"]["bedtime_circular_sd_min"], 10)

    def test_one_observation_does_not_claim_perfect_regularity(self):
        result = summarize_habits(
            self.profile, [record(1, rested=4)], datetime(2026, 2, 1, tzinfo=ZONE))
        first = result["primary"]["first_14_days"]
        self.assertIsNone(first["bedtime_circular_sd_min"])
        self.assertIsNone(first["wake_circular_sd_min"])

    def test_invalid_sleep_interval_and_subjective_values_are_bounded(self):
        invalid_interval = record(1, rested=5, fatigue=1)
        invalid_interval["bed"] = "not-a-date"
        rows = [
            invalid_interval,
            record(2, rested=float("nan"), fatigue=float("inf")),
            record(3, rested=6, fatigue=-1),
            record(4, rested=4, fatigue=3),
        ]
        result = summarize_habits(self.profile, rows, datetime(2026, 2, 1, tzinfo=ZONE))
        first = result["primary"]["first_14_days"]
        self.assertEqual(first["recorded_days"], 3)
        self.assertEqual(first["rested_response_days"], 1)
        self.assertEqual(first["fatigue_response_days"], 1)
        self.assertEqual(first["rested_mean"], 4.0)
        self.assertEqual(first["fatigue_mean"], 3.0)

    def test_missingness_is_explicit(self):
        rows = [record(1, rested=4), record(2, fatigue=2)]
        result = summarize_habits(self.profile, rows, datetime(2026, 2, 1, tzinfo=ZONE))
        first = result["primary"]["first_14_days"]
        self.assertEqual(first["missing_sleep_days"], 12)
        self.assertEqual(first["missing_rested_days"], 13)
        self.assertEqual(first["missing_fatigue_days"], 13)

    def test_incompatible_fatigue_scales_are_not_pooled(self):
        windows = record(1, fatigue=8)
        android = record(2, fatigue=4)
        android["subjective_scale"] = "android_rested_1_5_fatigue_1_5"
        result = summarize_habits(self.profile, [windows, android], datetime(2026, 2, 1, tzinfo=ZONE))
        first = result["primary"]["first_14_days"]
        self.assertFalse(first["fatigue_scales_comparable"])
        self.assertIsNone(first["fatigue_mean"])
        self.assertEqual(first["fatigue_response_days"], 2)
        self.assertEqual(set(first["fatigue_by_scale"]), {
            "legacy_diary_rested_1_5_fatigue_0_10", "android_rested_1_5_fatigue_1_5"})


if __name__ == "__main__":
    unittest.main()
