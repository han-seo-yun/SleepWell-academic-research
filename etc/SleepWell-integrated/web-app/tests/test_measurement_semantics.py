import copy
import sys
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import (PARAMETER_VERSION, VERSION, ZONE, derive_diary_measurement,
                    normalized_record_measurements, parameters, recommend,
                    validate_profile, validate_record, weighted_clock_mean)


NO_PRIOR = {"available": False, "artifact": None, "reason": "test", "source": "test"}


def profile(created_at):
    value = validate_profile({
        "name": "측정 의미 검증", "age": 25, "occupation": "worker",
        "usual_bed": "23:00", "usual_wake": "07:00", "target_wake": "07:00",
        "weekend_wake": "08:00", "usual_sleep_h": 7, "desired_sleep_h": 8,
        "average_steps": 6000, "exercise_min": 30, "latency_min": 20,
        "link_history": False,
    })
    value["created_at"] = created_at.isoformat()
    return value


def diary(wake, *, hours=8, latency=10, awake=20, rested=5, fatigue=2):
    return validate_record({
        "bed": (wake - timedelta(hours=hours)).isoformat(),
        "wake": wake.isoformat(),
        "latency_min": latency,
        "awake_min": awake,
        "rested": rested,
        "fatigue": fatigue,
    }, wake + timedelta(hours=1))


class DiaryMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.wake = datetime(2026, 2, 20, 8, tzinfo=ZONE)
        self.now = self.wake + timedelta(hours=2)

    def payload(self, **changes):
        value = {
            "bed": (self.wake - timedelta(hours=8)).isoformat(),
            "wake": self.wake.isoformat(),
        }
        value.update(changes)
        return value

    def test_missing_durations_are_distinct_from_explicit_zero(self):
        missing = validate_record(self.payload(), self.now)
        zero = validate_record(self.payload(latency_min=0, awake_min=0), self.now)
        latency_only = validate_record(self.payload(latency_min=15), self.now)
        awake_only = validate_record(self.payload(awake_min=15), self.now)
        self.assertEqual(missing["time_in_bed_min"], 480)
        self.assertIsNone(missing["sleep_min"])
        self.assertIsNone(missing["sleep_start"])
        self.assertEqual(missing["duration_status"], "missing_latency_and_awake")
        direct_zero = derive_diary_measurement(self.payload(latency_min=0, awake_min=0))
        self.assertEqual((zero["sleep_min"], direct_zero["sleep_min"]), (480, 480))
        self.assertEqual(zero["sleep_start"], zero["bed"])
        self.assertEqual(zero["duration_status"], "complete")
        self.assertIsNone(latency_only["sleep_min"])
        self.assertEqual(latency_only["sleep_start"],
                         (self.wake - timedelta(hours=8) + timedelta(minutes=15)).isoformat())
        self.assertEqual(latency_only["duration_status"], "missing_awake")
        self.assertIsNone(awake_only["sleep_min"])
        self.assertIsNone(awake_only["sleep_start"])
        self.assertEqual(awake_only["duration_status"], "missing_latency")

    def test_impossible_known_duration_is_rejected_when_other_field_is_missing(self):
        short_bed = (self.wake - timedelta(hours=2)).isoformat()
        for fields in ({"latency_min": 120}, {"awake_min": 120}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_record({"bed": short_bed, "wake": self.wake.isoformat(), **fields}, self.now)

    def test_saved_derived_values_are_recomputed_without_mutating_source(self):
        stored = diary(self.wake)
        stored.update({"record_id": "record-1", "sleep_min": 1,
                       "time_in_bed_min": 1, "sleep_start": stored["bed"]})
        original = copy.deepcopy(stored)
        normalized = normalized_record_measurements(stored)
        self.assertEqual(normalized["record_id"], "record-1")
        self.assertEqual(normalized["source"], "diary")
        self.assertEqual(normalized["time_in_bed_min"], 480)
        self.assertEqual(normalized["sleep_min"], 450)
        self.assertEqual(normalized["sleep_start"],
                         (self.wake - timedelta(hours=8) + timedelta(minutes=10)).isoformat())
        self.assertEqual(stored, original)
        without_availability = dict(stored)
        without_availability.pop("observed_at")
        without_availability.pop("received_at")
        derived = normalized_record_measurements(without_availability)
        self.assertNotIn("observed_at", derived)
        self.assertNotIn("received_at", derived)

    def test_quality_fields_and_collection_timestamps_are_normalized(self):
        survey_time = self.wake + timedelta(minutes=30)
        result = validate_record(self.payload(
            latency_min=0,
            awake_min=0,
            sleepiness=0,
            burden=10,
            survey_occurred_at=survey_time.isoformat(),
            received_at=(self.now + timedelta(days=1)).isoformat(),
            observed_at=(self.now + timedelta(days=1)).isoformat(),
        ), self.now)

        self.assertEqual(result["sleepiness"], 0)
        self.assertEqual(result["burden"], 10)
        self.assertEqual(result["survey_occurred_at"], survey_time.isoformat())
        self.assertEqual(result["received_at"], self.now.isoformat())
        self.assertEqual(result["observed_at"], self.now.isoformat())
        self.assertEqual(result["subjective_scales"]["sleepiness"], {"min": 0, "max": 10})
        self.assertEqual(result["subjective_scales"]["burden"], {"min": 0, "max": 10})
        self.assertEqual(result["subjective_scales"]["instrument"], "sleepwell_custom_not_validated")

        missing = validate_record(self.payload(latency_min=0, awake_min=0), self.now)
        self.assertIsNone(missing["sleepiness"])
        self.assertIsNone(missing["burden"])
        self.assertIsNone(missing["survey_occurred_at"])

    def test_survey_time_must_be_between_wake_and_receipt(self):
        for survey_time in (self.wake - timedelta(minutes=1), self.now + timedelta(minutes=1)):
            with self.subTest(survey_time=survey_time), self.assertRaises(ValueError):
                validate_record(self.payload(
                    latency_min=0,
                    awake_min=0,
                    survey_occurred_at=survey_time.isoformat(),
                ), self.now)

    def test_boolean_is_not_an_explicit_numeric_zero(self):
        for field in ("latency_min", "awake_min"):
            other = "awake_min" if field == "latency_min" else "latency_min"
            for value in (False, True):
                payload = self.payload(**{field: value, other: 0})
                with self.subTest(api="derive", field=field, value=value), self.assertRaises(ValueError):
                    derive_diary_measurement(payload)
                with self.subTest(api="validate", field=field, value=value), self.assertRaises(ValueError):
                    validate_record(payload, self.now)
        for field in ("sleepiness", "burden"):
            for value in (False, True):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    validate_record(self.payload(latency_min=0, awake_min=0, **{field: value}), self.now)

        string_zero = validate_record(self.payload(
            latency_min="0", awake_min="0", sleepiness="0", burden="0"), self.now)
        self.assertEqual(string_zero["duration_status"], "complete")
        self.assertEqual(string_zero["sleep_min"], 480)
        self.assertEqual(string_zero["sleepiness"], 0)
        self.assertEqual(string_zero["burden"], 0)


class DerivedPersonalizationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 2, 20, 20, tzinfo=ZONE)
        self.profile = profile(self.now - timedelta(days=40))

    def test_no_complete_duration_records_leave_observed_sleep_and_debt_unavailable(self):
        records = []
        for days_ago in range(6, 0, -1):
            wake = (self.now - timedelta(days=days_ago)).replace(hour=8)
            records.append(diary(wake, latency=10, awake=None))
        params = parameters(self.profile, records, self.now, population_prior=NO_PRIOR)
        flags = {flag["code"]: flag for flag in params["evidence_quality"]["flags"]}
        result = recommend(self.profile, records, self.now, population_prior=NO_PRIOR)
        self.assertTrue(flags["insufficient_effective_records"]["active"])
        self.assertLess(flags["insufficient_effective_records"]["observed"], 5)
        self.assertEqual(params["phase_status"], "profile_insufficient_evidence")
        self.assertEqual(params["phase_min"], 180)
        self.assertEqual(params["target_sleep_min"], 480)
        self.assertEqual(params["field_effective_records"]["sleep_target"], 0)
        self.assertIsNone(result["observed_sleep_min"])
        self.assertIsNone(result["sleep_debt_min"])
        self.assertEqual(result["sleep_debt_status"], "unavailable_no_complete_recent_diary")
        self.assertFalse(result["sleep_debt_applied"])

    def test_mixed_records_use_only_complete_raw_duration(self):
        complete = diary((self.now - timedelta(days=1)).replace(hour=8))
        latency_only = diary((self.now - timedelta(days=2)).replace(hour=8), awake=None)
        awake_only = diary((self.now - timedelta(days=3)).replace(hour=8), latency=None)
        complete["sleep_min"] = 1
        latency_only["sleep_min"] = 999
        awake_only["sleep_min"] = 999
        result = recommend(self.profile, [complete, latency_only, awake_only], self.now,
                           population_prior=NO_PRIOR)
        self.assertEqual(result["observed_sleep_min"], 450)
        self.assertEqual(result["sleep_debt_min"], 30)
        self.assertEqual(result["sleep_debt_status"], "available")
        self.assertFalse(result["sleep_debt_applied"])
        self.assertEqual(result["target_sleep_min"], 480)

    def test_future_and_unknown_observation_times_are_excluded(self):
        valid = diary((self.now - timedelta(days=1)).replace(hour=8))
        future_observation = diary((self.now - timedelta(days=2)).replace(hour=8))
        future_observation["observed_at"] = (self.now + timedelta(minutes=1)).isoformat()
        unknown_observation = diary((self.now - timedelta(days=3)).replace(hour=8))
        unknown_observation.pop("observed_at")

        result = parameters(self.profile, [valid, future_observation, unknown_observation],
                            self.now, population_prior=NO_PRIOR)

        self.assertEqual(result["valid_days"], 1)
        self.assertEqual(result["unknown_observation_time_excluded_count"], 1)

    def test_circular_cancellation_and_irregular_history_keep_profile_schedule(self):
        center, resultant = weighted_clock_mean([0, 720], [1, 1])
        self.assertIsNone(center)
        self.assertLess(resultant, 1e-12)

        records = []
        for index, days_ago in enumerate(range(8, 0, -1)):
            wake_hour = 4 if index % 2 == 0 else 16
            wake_date = (self.now - timedelta(days=days_ago)).date()
            wake = datetime.combine(wake_date, time(wake_hour), ZONE)
            records.append(diary(wake, latency=0, awake=0))
        params = parameters(self.profile, records, self.now, population_prior=NO_PRIOR)
        flags = {flag["code"]: flag for flag in params["evidence_quality"]["flags"]}
        result = recommend(self.profile, records, self.now, population_prior=NO_PRIOR)

        self.assertFalse(flags["insufficient_effective_records"]["active"])
        self.assertFalse(flags["stale_history"]["active"])
        self.assertTrue(flags["irregular_midpoint"]["active"])
        self.assertLess(params["phase_resultant_length"], 0.5)
        self.assertEqual(params["phase_status"], "profile_irregular_history")
        self.assertEqual(params["phase_min"], 180)
        self.assertEqual(params["target_sleep_min"], 480)
        self.assertEqual(params["evidence_quality"]["status"], "conservative")
        self.assertTrue(params["evidence_quality"]["heuristic_not_calibrated_probability"])
        self.assertIsNone(params["uncertainty"]["prediction_interval"])
        self.assertEqual(params["uncertainty"]["prediction_interval_status"], "unvalidated")
        self.assertFalse(result["sleep_debt_applied"])
        self.assertEqual(result["target_sleep_min"], params["target_sleep_min"])

    def test_versions_describe_measurement_and_evidence_semantics(self):
        self.assertEqual(VERSION, "sleep-opportunity-v7-measurement-evidence")
        self.assertEqual(PARAMETER_VERSION, "personalization-v5-measurement-evidence")

    def test_backfilled_old_events_remain_stale_despite_recent_receipt(self):
        backfill_profile = profile(self.now - timedelta(days=90))
        records = []
        for days_ago in range(15, 45):
            wake = (self.now - timedelta(days=days_ago)).replace(hour=10)
            item = diary(wake, hours=10, latency=10, awake=20)
            item["observed_at"] = self.now.isoformat()
            item["received_at"] = self.now.isoformat()
            records.append(item)

        params = parameters(backfill_profile, records, self.now, population_prior=NO_PRIOR)
        flags = {flag["code"]: flag for flag in params["evidence_quality"]["flags"]}

        self.assertGreater(params["effective_records"], 5)
        self.assertGreater(params["last_record_gap_days"], 14)
        self.assertEqual(params["latest_observation_age_days"], 0)
        self.assertTrue(flags["stale_history"]["active"])
        self.assertAlmostEqual(flags["stale_history"]["observed"],
                               params["last_record_gap_days"])
        self.assertEqual(params["phase_status"], "profile_stale_history")
        self.assertEqual(params["phase_min"], 180)
        self.assertEqual(params["target_sleep_min"], 480)
        self.assertEqual(params["evidence_quality"]["status"], "conservative")


if __name__ == "__main__":
    unittest.main()
