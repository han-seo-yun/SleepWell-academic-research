from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

import evaluate


KST = timezone(timedelta(hours=9))
UTC = timezone.utc


class SamsungTimeBoundaryTests(unittest.TestCase):
    def test_naive_samsung_clock_is_utc_then_explicit_offset(self):
        parsed = evaluate.parse_samsung_time(
            "2026-10-23 14:39:00.000", "UTC+0900"
        )
        self.assertEqual(parsed.isoformat(), "2026-10-23T23:39:00+09:00")
        self.assertEqual(parsed.astimezone(UTC).isoformat(), "2026-10-23T14:39:00+00:00")

    def test_midnight_start_uses_previous_local_20_cutoff(self):
        start = datetime(2026, 10, 24, 0, 15, tzinfo=KST)
        cutoff = evaluate.decision_cutoff(start)
        self.assertEqual(cutoff.isoformat(), "2026-10-23T20:00:00+09:00")
        self.assertEqual(evaluate.offset_minutes(start, cutoff), 255.0)


class DeduplicationTests(unittest.TestCase):
    def session(self, source: str, update_hour: int, reported: float):
        return evaluate.SamsungSession(
            participant_id="P02",
            record_id="same-samsung-uuid",
            start=datetime(2026, 10, 1, 23, 0, tzinfo=KST),
            end=datetime(2026, 10, 2, 7, 0, tzinfo=KST),
            reported_sleep_min=reported,
            updated_at=datetime(2026, 10, 2, update_hour, 0, tzinfo=KST),
            time_offset_min=540,
            source_paths=(source,),
            export_ids=(source,),
            raw_record_ids=("same-samsung-uuid",),
        )

    def test_duplicate_exports_choose_latest_and_union_provenance(self):
        earlier = self.session("older-export.csv", 8, 420.0)
        later = self.session("newer-export.csv", 9, 430.0)
        rows, audit = evaluate.deduplicate_sessions([earlier, later])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].reported_sleep_min, 430.0)
        self.assertEqual(
            rows[0].source_paths, ("newer-export.csv", "older-export.csv")
        )
        self.assertEqual(audit["duplicates_removed"], 1)


class AvailabilityAndLeakageTests(unittest.TestCase):
    def survey(self, saved_at: datetime, row_number: int):
        return evaluate.SurveyRecord(
            participant_id="P03",
            study_date=date(2026, 10, 14),
            saved_at=saved_at,
            values={"fatigue": "5"},
            source_path="survey.csv",
            row_number=row_number,
        )

    def test_only_latest_strictly_pre_cutoff_survey_is_available(self):
        cutoff = datetime(2026, 10, 14, 20, 0, tzinfo=KST)
        early = self.survey(datetime(2026, 10, 14, 10, 0, tzinfo=UTC), 2)
        exact = self.survey(datetime(2026, 10, 14, 11, 0, tzinfo=UTC), 3)
        late = self.survey(datetime(2026, 10, 14, 12, 0, tzinfo=UTC), 4)
        available = evaluate.survey_at_cutoff(
            [early, exact, late], "P03", cutoff, available_only=True
        )
        retrospective = evaluate.survey_at_cutoff(
            [early, exact, late], "P03", cutoff, available_only=False
        )
        self.assertIs(available, early)
        self.assertIs(retrospective, late)

    def test_history_requires_both_wake_and_observed_strictly_before_cutoff(self):
        cutoff = datetime(2026, 10, 14, 20, 0, tzinfo=KST)
        valid = {
            "wake": "2026-10-14T07:00:00+09:00",
            "observed_at": "2026-10-14T07:01:00+09:00",
        }
        backfilled_at_cutoff = {
            "wake": "2026-10-13T07:00:00+09:00",
            "observed_at": cutoff.isoformat(),
        }
        future_outcome = {
            "wake": "2026-10-15T07:00:00+09:00",
            "observed_at": "2026-10-15T07:01:00+09:00",
        }
        history = evaluate.eligible_history(
            [future_outcome, backfilled_at_cutoff, valid], cutoff
        )
        self.assertEqual(history, [valid])


if __name__ == "__main__":
    unittest.main()
