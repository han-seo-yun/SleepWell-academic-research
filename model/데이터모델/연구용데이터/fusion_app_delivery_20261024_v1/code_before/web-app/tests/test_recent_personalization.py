import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import ZONE, clock_delta, parameters, validate_profile, validate_record


NO_PRIOR = {"available": False, "artifact": None, "reason": "test", "source": "test"}


def profile(start):
    value = validate_profile({"name": "최근성 검증", "age": 25, "occupation": "worker",
        "usual_bed": "23:30", "usual_wake": "07:00", "target_wake": "07:00",
        "weekend_wake": "08:00", "usual_sleep_h": 7, "desired_sleep_h": 8,
        "average_steps": 6000, "exercise_min": 30, "latency_min": 20,
        "link_history": False})
    value["created_at"] = start.isoformat()
    return value


def record(wake, hours=8, steps=8000, latency=10, exercise=40):
    return validate_record({"bed": (wake - timedelta(hours=hours)).isoformat(), "wake": wake.isoformat(),
        "latency_min": latency, "awake_min": 0, "rested": 5, "fatigue": 2,
        "steps": steps, "exercise_min": exercise}, wake + timedelta(minutes=1))


class RecentPersonalizationTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, 12, tzinfo=ZONE)
        self.now = datetime(2026, 4, 1, 12, tzinfo=ZONE)
        self.profile = profile(self.start)

    def test_recent_records_outweigh_stale_records_and_midnight_phase_is_circular(self):
        stale = [record(self.now - timedelta(days=70 - i), steps=1000) for i in range(20)]
        recent = [record((self.now - timedelta(days=14 - i)).replace(hour=8), steps=10000) for i in range(14)]
        result = parameters(self.profile, stale + recent, self.now, population_prior=NO_PRIOR)
        self.assertGreater(result["average_steps"], 7000)
        self.assertLess(abs(clock_delta(result["phase_min"], 240)), 90)
        self.assertEqual(result["cumulative_valid_days"], 34)
        self.assertEqual(result["recent_days"], 13)

    def test_gap_reduces_effective_count_and_personal_weight(self):
        recent = [record(self.now - timedelta(days=i + 1), steps=10000) for i in range(14)]
        stale = [record(self.now - timedelta(days=i + 61), steps=10000) for i in range(14)]
        recent_result = parameters(self.profile, recent, self.now, population_prior=NO_PRIOR)
        stale_result = parameters(self.profile, stale, self.now, population_prior=NO_PRIOR)
        self.assertEqual(stale_result["valid_days"], 14)
        self.assertEqual(stale_result["mode"], "personalized")
        self.assertLess(stale_result["effective_records"], recent_result["effective_records"] / 8)
        self.assertLess(stale_result["personalization_weight"], recent_result["personalization_weight"])
        self.assertGreater(stale_result["last_record_gap_days"], 60)

    def test_gate_remains_14_elapsed_and_14_valid_records(self):
        records = [record(self.start + timedelta(days=i + 1)) for i in range(14)]
        self.assertEqual(parameters(self.profile, records[:13], self.now, population_prior=NO_PRIOR)["mode"], "initial")
        too_early = self.start + timedelta(days=13, hours=20)
        self.assertEqual(parameters(self.profile, records, too_early, population_prior=NO_PRIOR)["mode"], "initial")
        result = parameters(self.profile, records, self.now, population_prior=NO_PRIOR)
        self.assertEqual(result["mode"], "personalized")
        self.assertEqual(result["recency_half_life_days"], 14)
        self.assertIn("기상 후 경과일", result["confidence_note"])

    def test_late_edit_uses_wake_age_future_is_excluded_and_missing_fields_have_own_weight(self):
        records = [record(self.now - timedelta(days=i + 1), steps=10000) for i in range(14)]
        for item in records[1:]:
            item["steps"] = None
        records[0]["observed_at"] = (self.now - timedelta(minutes=1)).isoformat()
        future = record(self.now + timedelta(days=1), steps=100000)
        future["observed_at"] = (self.now - timedelta(minutes=1)).isoformat()
        result = parameters(self.profile, records + [future], self.now, population_prior=NO_PRIOR)
        self.assertEqual(result["valid_days"], 14)
        self.assertLess(result["average_steps"], 6300)
        self.assertEqual(result["last_record_gap_days"], 1)
        self.assertIn("값이 있는 1일", result["origins"]["activity"])
        self.assertLess(result["field_effective_records"]["activity"], 1)

    def test_ancient_records_with_underflowed_weights_do_not_crash(self):
        ancient_start = datetime(1900, 1, 1, 12, tzinfo=ZONE)
        ancient_profile = profile(ancient_start)
        records = [record(datetime(1900, 2, 1, 8, tzinfo=ZONE) + timedelta(days=i)) for i in range(14)]
        result = parameters(ancient_profile, records, self.now, population_prior=NO_PRIOR)
        self.assertEqual(result["mode"], "personalized")
        self.assertEqual(result["effective_records"], 0)
        self.assertEqual(result["personalization_weight"], 0)
        self.assertEqual(result["average_steps"], 6000)


if __name__ == "__main__":
    unittest.main()
