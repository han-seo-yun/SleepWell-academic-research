import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import ZONE, parameters, validate_profile, validate_record
from personalization import load_population_prior, select_population_prior


def artifact(groups=None, global_n=500, global_mean=455):
    return {
        "schema_version": 1,
        "artifact_type": "sleepwell_population_prior",
        "version": "test-v1",
        "source": "test population; observed duration, not optimal need",
        "supported_age": [18, 80],
        "groups": groups or [],
        "global": {"n_people": global_n, "mean_sleep_min": global_mean},
        "unsupported_fields": ["occupation", "usual_bed"],
    }


def profile(start, **changes):
    payload = {"name": "개인화 검증", "age": 25, "sex": "female", "occupation": "worker",
               "activity_hours_per_day": 0.5, "usual_bed": "23:30", "usual_wake": "07:00",
               "target_wake": "07:00", "weekend_wake": "08:00", "usual_sleep_h": 7,
               "desired_sleep_h": 8, "average_steps": 6000, "exercise_min": 30,
               "latency_min": 20, "link_history": False}
    payload.update(changes)
    result = validate_profile(payload)
    result["created_at"] = start.isoformat()
    return result


def record(wake, observed_at=None, steps=8000, rested=5):
    result = validate_record({"bed": (wake - timedelta(hours=8)).isoformat(), "wake": wake.isoformat(),
                              "latency_min": 10, "awake_min": 0, "rested": rested, "fatigue": 2,
                              "steps": steps, "exercise_min": 40}, wake + timedelta(hours=1))
    if observed_at is not None:
        result["observed_at"] = observed_at.isoformat()
    return result


class PopulationPriorTests(unittest.TestCase):
    def test_narrow_group_then_supported_fallback_order(self):
        groups = [
            {"age_group": "18-29", "sex": "female", "activity_band": "medium", "n_people": 29, "mean_sleep_min": 470},
            {"age_group": "18-29", "sex": "female", "activity_band": "any", "n_people": 35, "mean_sleep_min": 465},
            {"age_group": "18-29", "sex": "any", "activity_band": "any", "n_people": 100, "mean_sleep_min": 460},
        ]
        loaded = {"available": True, "artifact": artifact(groups), "reason": "ok", "source": "test"}
        selected = select_population_prior(profile(datetime(2026, 1, 1, tzinfo=ZONE)), loaded)
        self.assertEqual(selected["level"], "18-29/female/any")
        self.assertEqual(selected["n_people"], 35)

    def test_missing_activity_uses_any_group_without_inference(self):
        groups = [
            {"age_group": "18-29", "sex": "female", "activity_band": "low", "n_people": 50, "mean_sleep_min": 470},
            {"age_group": "18-29", "sex": "female", "activity_band": "any", "n_people": 60, "mean_sleep_min": 460},
        ]
        loaded = {"available": True, "artifact": artifact(groups), "reason": "ok", "source": "test"}
        candidate = profile(datetime(2026, 1, 1, tzinfo=ZONE))
        candidate["activity_hours_per_day"] = None
        selected = select_population_prior(candidate, loaded)
        self.assertEqual(selected["level"], "18-29/female/any")
        self.assertEqual(selected["mean_sleep_min"], 460)

    def test_initial_duration_uses_observed_mean_with_age_bounds(self):
        start = datetime(2026, 1, 1, 12, tzinfo=ZONE)
        groups = [{"age_group": "18-29", "sex": "female", "activity_band": "medium",
                   "n_people": 50, "mean_sleep_min": 420}]
        loaded = {"available": True, "artifact": artifact(groups), "reason": "ok", "source": "test"}
        result = parameters(profile(start), [], start, population_prior=loaded)
        self.assertEqual(result["target_sleep_min"], 420)  # population mean, clipped only to adult age bound
        self.assertTrue(result["population_prior"]["available"])
        self.assertIn("최적 필요 수면시간을 뜻하지 않음", result["origins"]["sleep_target"])

    def test_personal_update_is_bounded_from_population_baseline(self):
        start = datetime(2026, 1, 1, 12, tzinfo=ZONE)
        groups = [{"age_group": "18-29", "sex": "female", "activity_band": "medium",
                   "n_people": 50, "mean_sleep_min": 420}]
        loaded = {"available": True, "artifact": artifact(groups), "reason": "ok", "source": "test"}
        records = [record(start.replace(hour=8) + timedelta(days=i + 1)) for i in range(14)]
        for item in records:
            item["sleep_min"] = 600
        result = parameters(profile(start), records, start + timedelta(days=15), population_prior=loaded)
        self.assertEqual(result["target_sleep_min"], 450)
        self.assertIn("초기 기준값 ±30분", result["origins"]["sleep_target"])

    def test_malformed_artifact_fails_closed_and_old_profile_defaults(self):
        malformed = [
            ["not", "an", "object"],
            dict(artifact(), groups=[[]]),
            {**artifact(), "global": {"n_people": True, "mean_sleep_min": 455}},
            dict(artifact(), groups=[
                {"age_group": "18-29", "sex": "any", "activity_band": "any", "n_people": 30, "mean_sleep_min": 455},
                {"age_group": "18-29", "sex": "any", "activity_band": "any", "n_people": 31, "mean_sleep_min": 456},
            ]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for value in malformed:
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assertFalse(load_population_prior(path)["available"])
            path.write_text('{"schema_version": 1}', encoding="utf-8")
            loaded = load_population_prior(path)
        start = datetime(2026, 1, 1, 12, tzinfo=ZONE)
        old = profile(start)
        old.pop("sex")
        old.pop("activity_hours_per_day")
        result = parameters(old, [], start, population_prior=loaded)
        self.assertEqual(result["target_sleep_min"], 480)
        self.assertFalse(result["population_prior"]["available"])
        self.assertIn("안전하게 읽지 못해", result["population_prior"]["reason"])

    def test_profile_input_fields_are_optional_for_legacy_clients(self):
        start = datetime(2026, 1, 1, 12, tzinfo=ZONE)
        legacy = profile(start)
        payload = {key: value for key, value in legacy.items()
                   if key not in {"created_at", "sex", "activity_hours_per_day"}}
        validated = validate_profile(payload)
        self.assertEqual(validated["sex"], "unspecified")
        self.assertIsNone(validated["activity_hours_per_day"])

    def test_out_of_supported_age_uses_declared_profile(self):
        start = datetime(2026, 1, 1, 12, tzinfo=ZONE)
        loaded = {"available": True, "artifact": artifact(), "reason": "ok", "source": "test"}
        result = parameters(profile(start, age=17, desired_sleep_h=9), [], start, population_prior=loaded)
        self.assertEqual(result["target_sleep_min"], 540)
        self.assertFalse(result["population_prior"]["available"])
        self.assertIn("지원 범위", result["population_prior"]["reason"])


class CumulativePersonalizationTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, 12, tzinfo=ZONE)
        self.profile = profile(self.start)
        self.no_prior = {"available": False, "artifact": None, "reason": "test no artifact", "source": "test"}

    def test_records_older_than_28_days_remain_in_cumulative_update(self):
        records = [record(self.start.replace(hour=8) + timedelta(days=i + 1)) for i in range(40)]
        result = parameters(self.profile, records, self.start + timedelta(days=60), population_prior=self.no_prior)
        self.assertEqual(result["cumulative_valid_days"], 40)
        self.assertLess(result["effective_records"], 10)
        self.assertLess(result["personalization_weight"], 0.42)
        self.assertEqual(result["mode"], "personalized")

    def test_same_date_correction_is_counted_once_and_latest_wins(self):
        records = [record(self.start.replace(hour=8) + timedelta(days=i + 1), steps=6000) for i in range(14)]
        original = records[-1]
        corrected = dict(original, steps=20000,
                         observed_at=(datetime.fromisoformat(original["observed_at"]) + timedelta(hours=1)).isoformat())
        result = parameters(self.profile, records + [corrected], self.start + timedelta(days=15), population_prior=self.no_prior)
        self.assertEqual(result["valid_days"], 14)
        # The newest correction has the highest recency weight, while the initial value still shrinks the estimate.
        self.assertGreater(result["average_steps"], 6000)
        self.assertLess(result["average_steps"], 7000)

    def test_same_timestamp_correction_is_order_independent(self):
        records = [record(self.start.replace(hour=8) + timedelta(days=i + 1), steps=6000) for i in range(14)]
        first = dict(records[-1], steps=7000)
        second = dict(records[-1], steps=9000)
        now = self.start + timedelta(days=15)
        forward = parameters(self.profile, records[:-1] + [first, second], now, population_prior=self.no_prior)
        reverse = parameters(self.profile, records[:-1] + [second, first], now, population_prior=self.no_prior)
        self.assertEqual(forward["average_steps"], reverse["average_steps"])
        self.assertEqual(forward["valid_days"], 14)

    def test_future_wake_and_future_observation_are_excluded(self):
        now = self.start + timedelta(days=20)
        valid = record(now - timedelta(days=1))
        future_wake = record(now + timedelta(days=1))
        late_observation = record(now - timedelta(days=2), observed_at=now + timedelta(minutes=1))
        result = parameters(self.profile, [valid, future_wake, late_observation], now, population_prior=self.no_prior)
        self.assertEqual(result["valid_days"], 1)

    def test_missing_observed_at_is_kept_only_as_explicit_legacy_compatibility(self):
        legacy = record(self.start + timedelta(days=1))
        legacy.pop("observed_at")
        result = parameters(self.profile, [legacy], self.start + timedelta(days=2), population_prior=self.no_prior)
        self.assertEqual(result["valid_days"], 1)
        self.assertEqual(result["legacy_compatible_days"], 1)
        self.assertIn("호환 목적", result["note"])


if __name__ == "__main__":
    unittest.main()
