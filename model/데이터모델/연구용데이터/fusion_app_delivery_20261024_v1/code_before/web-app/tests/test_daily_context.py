import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daily_context import validate_daily_context
from engine import ZONE, plan_night_id, recommend, validate_profile


def profile(now):
    value = validate_profile({"name": "일정 검증", "age": 25, "occupation": "worker",
        "usual_bed": "23:30", "usual_wake": "07:00", "target_wake": "07:00",
        "weekend_wake": "08:00", "usual_sleep_h": 7, "desired_sleep_h": 8,
        "average_steps": 6000, "exercise_min": 30, "latency_min": 20,
        "link_history": False})
    value["created_at"] = now.isoformat()
    return value


class DailyContextTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 20, 19, 30, tzinfo=ZONE)

    def valid(self):
        return {"night_id": "2026-10-20", "preparation_min": 30,
                "schedules": [{"kind": "class", "start": "2026-10-21T06:00:00+09:00", "end": None}],
                "naps": [{"start": "2026-10-20T13:00:00+09:00", "end": "2026-10-20T14:00:00+09:00"}],
                "caffeine": [{"at": "2026-10-20T15:00:00+09:00", "mg": 120}]}

    def test_normalizes_snapshot_and_missing_sections(self):
        result = validate_daily_context(self.valid(), self.now)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["observed_at"], self.now.isoformat())
        self.assertEqual(result["caffeine"][0]["mg"], 120)
        minimal = validate_daily_context({"night_id": "2026-10-20"}, self.now)
        self.assertEqual(minimal["schedules"], [])
        self.assertEqual(minimal["naps"], [])
        self.assertEqual(minimal["caffeine"], [])

    def test_rejects_malformed_limits_future_events_and_overlapping_naps(self):
        bad_values = [
            {"night_id": "2026/10/20"},
            {"night_id": "2026-11-20"},
            {"night_id": "2026-10-20", "preparation_min": True},
            {"night_id": "2026-10-20", "caffeine": [{"at": "2026-10-20T15:00:00+09:00", "mg": float("nan")}]},
            {"night_id": "2026-10-20", "naps": [{"start": "2026-10-20T18:00:00+09:00", "end": "2026-10-20T20:00:00+09:00"}]},
            {"night_id": "2026-10-20", "schedules": [{"kind": "work", "start": "2026-10-20T19:00:00+09:00"}]},
            {"night_id": "2026-10-20", "naps": [
                {"start": "2026-10-20T12:00:00+09:00", "end": "2026-10-20T14:00:00+09:00"},
                {"start": "2026-10-20T13:00:00+09:00", "end": "2026-10-20T15:00:00+09:00"}]},
        ]
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_daily_context(value, self.now)

    def test_plan_night_id_handles_remainder_after_midnight(self):
        p = profile(self.now)
        early = datetime(2026, 10, 21, 1, tzinfo=ZONE)
        self.assertEqual(plan_night_id(p, self.now), "2026-10-20")
        self.assertEqual(plan_night_id(p, early), "2026-10-20")

    def test_schedule_constrains_wake_nap_enters_pressure_and_caffeine_is_snapshot_only(self):
        p = profile(self.now)
        context = self.valid()
        with_context = recommend(p, [], self.now, daily_context=context)
        without_context = recommend(p, [], self.now)
        self.assertEqual(datetime.fromisoformat(with_context["wake"]).strftime("%H:%M"), "05:30")
        self.assertTrue(with_context["context_flags"]["schedule_constraint_applied"])
        self.assertTrue(with_context["context_flags"]["naps_applied"])
        self.assertTrue(with_context["context_flags"]["caffeine_recorded"])
        self.assertLess(with_context["current_s"], without_context["current_s"])
        caffeine_reason = next(reason for reason in with_context["reasons"] if "카페인" in reason)
        self.assertIn("계수가 없어", caffeine_reason)
        self.assertIn("사용하지 않았습니다", caffeine_reason)

    def test_impossible_early_schedule_is_reported(self):
        now = self.now.replace(hour=23)
        p = profile(self.now)
        context = {"night_id": "2026-10-20", "preparation_min": 60,
                   "schedules": [{"kind": "first", "start": "2026-10-21T04:00:00+09:00"}]}
        result = recommend(p, [], now, daily_context=context)
        self.assertFalse(result["feasible"])
        self.assertTrue(result["context_flags"]["schedule_infeasible"])

    def test_context_for_another_night_is_rejected(self):
        p = profile(self.now)
        with self.assertRaisesRegex(ValueError, "현재 계획 밤"):
            recommend(p, [], self.now, daily_context={"night_id": "2026-10-19"})

    def test_stored_snapshot_provenance_is_preserved_and_future_snapshot_rejected(self):
        p = profile(self.now)
        context = self.valid()
        context.update(observed_at="2026-10-20T18:00:00+09:00", revision_id=7)
        result = recommend(p, [], self.now, daily_context=context)
        self.assertEqual(result["daily_context"]["observed_at"], "2026-10-20T18:00:00+09:00")
        self.assertEqual(result["daily_context"]["revision_id"], 7)
        context["observed_at"] = "2026-10-20T20:00:00+09:00"
        with self.assertRaisesRegex(ValueError, "미래"):
            recommend(p, [], self.now, daily_context=context)

    def test_same_evening_first_event_does_not_become_next_morning_wake(self):
        p = profile(self.now)
        context = {"night_id": "2026-10-20", "preparation_min": 60,
                   "schedules": [{"kind": "first", "start": "2026-10-20T22:00:00+09:00"}]}
        result = recommend(p, [], self.now, daily_context=context)
        self.assertEqual(datetime.fromisoformat(result["wake"]).strftime("%Y-%m-%d %H:%M"), "2026-10-21 07:00")
        self.assertFalse(result["context_flags"]["schedule_constraint_applied"])

    def test_overnight_work_blocks_sleep_and_reports_insufficient_window(self):
        p = profile(self.now)
        context = {"night_id": "2026-10-20", "preparation_min": 60,
                   "schedules": [{"kind": "work", "start": "2026-10-20T22:00:00+09:00",
                                  "end": "2026-10-21T08:00:00+09:00"}]}
        result = recommend(p, [], self.now, daily_context=context)
        self.assertFalse(result["feasible"])
        self.assertTrue(result["context_flags"]["schedule_infeasible"])
        self.assertEqual(datetime.fromisoformat(result["wake"]).strftime("%H:%M"), "22:00")
        self.assertGreaterEqual(result["sleep_opportunity_min"], 0)
        self.assertLessEqual(datetime.fromisoformat(result["bed"]), datetime.fromisoformat(result["wake"]))

    def test_ongoing_occupied_interval_is_not_recommended_as_sleep(self):
        now = datetime(2026, 10, 21, 1, 0, tzinfo=ZONE)
        p = profile(self.now)
        context = {"night_id": "2026-10-20", "schedules": [
            {"kind": "work", "start": "2026-10-20T22:00:00+09:00", "end": "2026-10-21T06:00:00+09:00"}]}
        result = recommend(p, [], now, daily_context=context)
        self.assertGreaterEqual(datetime.fromisoformat(result["bed"]), datetime(2026, 10, 21, 6, tzinfo=ZONE))
        self.assertFalse(result["feasible"])

    def test_next_day_class_caps_wake_but_same_time_as_profile_is_coherent(self):
        p = profile(self.now)
        context = {"night_id": "2026-10-20", "preparation_min": 60,
                   "schedules": [{"kind": "class", "start": "2026-10-21T08:00:00+09:00",
                                  "end": "2026-10-21T10:00:00+09:00"}]}
        result = recommend(p, [], self.now, daily_context=context)
        self.assertEqual(datetime.fromisoformat(result["wake"]).strftime("%H:%M"), "07:00")
        self.assertGreaterEqual(result["sleep_opportunity_min"], 0)


if __name__ == "__main__":
    unittest.main()
