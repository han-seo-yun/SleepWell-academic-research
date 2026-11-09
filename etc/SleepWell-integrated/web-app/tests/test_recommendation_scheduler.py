import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from daily_context import validate_daily_context
from engine import ZONE
from recommendation_schedule import public_schedule, schedule
from server import Application, Store
from test_prospective_store import Research, profile


class RecommendationSchedulerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "scheduler.sqlite3")
        self.clock = datetime(2026, 10, 28, 20, 5, tzinfo=ZONE)
        self.store.save_profile(profile(), [], self.clock - timedelta(days=20))
        self.app = Application(self.store, Research(), clock=lambda: self.clock)
        timing = public_schedule(schedule("2026-10-28", datetime(2026, 10, 28, 23, 0, tzinfo=ZONE)))
        timing.update(created_at=self.clock.isoformat(), expected_onset_source="test",
                      role="operational_display_timing_only")
        self.store.save_recommendation_schedule(timing)

    def tearDown(self):
        self.temp.cleanup()

    def observe_and_check(self):
        self.store.save_awake_observation("2026-10-28", self.clock)
        return self.app.check_recommendation_schedule(self.clock)

    def test_initial_hourly_dedup_changed_only_and_frozen_forecast(self):
        first = self.observe_and_check()
        self.assertEqual(first["slot"], "initial")
        self.assertEqual(first["status"], "published")
        frozen = self.store.prospective_forecasts()[0]
        self.assertEqual(self.observe_and_check()["id"], first["id"])

        self.clock = self.clock.replace(hour=21)
        unchanged = self.observe_and_check()
        self.assertEqual(unchanged["slot"], "hourly_21")
        self.assertEqual(unchanged["status"], "unchanged")

        self.clock = self.clock.replace(hour=21, minute=10)
        self.store.save_context(validate_daily_context({
            "night_id": "2026-10-28", "preparation_min": 30,
            "schedules": [{"kind": "class", "start": "2026-10-29T08:00",
                           "end": "2026-10-29T09:00"}],
            "naps": [], "caffeine": []}, self.clock))
        self.clock = self.clock.replace(hour=22, minute=5)
        changed = self.observe_and_check()
        self.assertEqual(changed["slot"], "hourly_22")
        self.assertEqual(changed["status"], "published")
        self.assertEqual(self.store.prospective_forecasts()[0], frozen)

    def test_samsung_auxiliary_record_does_not_trigger_revision_hash(self):
        before = self.app._recommendation_input(self.clock, "2026-10-28")
        self.store.save_record({
            "date": "2026-10-28", "bed": "2026-10-27T23:00:00+09:00",
            "wake": "2026-10-28T07:00:00+09:00", "observed_at": self.clock.isoformat(),
            "source": "health_connect", "record_id": "wearable-only"
        }, self.clock)
        after = self.app._recommendation_input(self.clock, "2026-10-28")
        self.assertEqual(after, before)

    def test_no_fresh_awake_signal_fails_closed(self):
        decision = self.app.check_recommendation_schedule(self.clock)
        self.assertEqual(decision["status"], "no_fresh_awake_signal")
        self.assertEqual(self.store.predictions(), [])


if __name__ == "__main__":
    unittest.main()
