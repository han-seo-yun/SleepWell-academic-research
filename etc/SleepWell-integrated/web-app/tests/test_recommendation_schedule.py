import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from recommendation_schedule import due_slot, public_schedule, schedule

ZONE = ZoneInfo("Asia/Seoul")


class RecommendationScheduleTest(unittest.TestCase):
    def test_early_expected_onset_moves_initial_before_20(self):
        timing = schedule("2026-10-28", datetime(2026, 10, 28, 21, 0, tzinfo=ZONE))
        self.assertEqual(timing["initial_decision_at"].isoformat(), "2026-10-28T19:00:00+09:00")
        self.assertEqual(public_schedule(timing)["model_cutoff"], "2026-10-28T20:00:00+09:00")

    def test_late_expected_onset_caps_initial_at_20(self):
        timing = schedule("2026-10-28", datetime(2026, 10, 28, 23, 0, tzinfo=ZONE))
        self.assertEqual(timing["initial_decision_at"].isoformat(), "2026-10-28T20:00:00+09:00")

    def test_initial_at_20_consumes_hourly_20_collision(self):
        timing = schedule("2026-10-28", datetime(2026, 10, 28, 23, 0, tzinfo=ZONE))
        due = due_slot(datetime(2026, 10, 28, 20, 5, tzinfo=ZONE), timing, awake=True)
        self.assertEqual(due["slot"], "initial")
        self.assertEqual(due["kind"], "personalized_initial")
        self.assertIsNone(due_slot(datetime(2026, 10, 28, 20, 15, tzinfo=ZONE), timing,
                                   seen_slots={"initial"}, awake=True))

    def test_initial_is_due_only_before_cutoff_and_is_not_backfilled(self):
        timing = schedule("2026-10-28", datetime(2026, 10, 28, 21, 0, tzinfo=ZONE))
        due = due_slot(datetime(2026, 10, 28, 19, 10, tzinfo=ZONE), timing, awake=True)
        self.assertEqual(due["slot"], "initial")
        self.assertIsNone(due_slot(datetime(2026, 10, 28, 19, 10, tzinfo=ZONE), timing,
                                   seen_slots={"initial"}, awake=True))
        self.assertEqual(due_slot(datetime(2026, 10, 28, 21, 10, tzinfo=ZONE), timing,
                                  awake=True)["slot"], "hourly_21")

    def test_hourly_revision_requires_awake_evidence_and_deduplicates(self):
        timing = schedule("2026-10-28", datetime(2026, 10, 28, 23, 0, tzinfo=ZONE))
        now = datetime(2026, 10, 28, 22, 35, tzinfo=ZONE)
        self.assertIsNone(due_slot(now, timing, awake=False))
        self.assertEqual(due_slot(now, timing, awake=True)["slot"], "hourly_22")
        self.assertIsNone(due_slot(now, timing, seen_slots={"hourly_22"}, awake=True))
        self.assertIsNone(due_slot(datetime(2026, 10, 29, 0, 5, tzinfo=ZONE), timing, awake=True))


if __name__ == "__main__":
    unittest.main()
